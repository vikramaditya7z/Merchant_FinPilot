"""Parser and validator for LLM Agent responses.

PROJECT_RULES 1.6, 4.2, 10.8 / ARCHITECTURE.md §8, §17.

Guarantees:
- Treats model output as untrusted user input.
- Extracts clean JSON even when wrapped in markdown fences.
- Validates and constructs typed AgentStructuredFinding and AgentIntent objects.
- Enforces domain invariants (exact types, reason length, evidence citations).
"""

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..domain.canonical import short_digest
from ..domain.enums import Currency, IntentAction, TargetEntityType
from ..domain.errors import DomainValidationError
from ..domain.intent import AgentIntent, IntentTarget
from ..domain.money import Money
from ..domain.window import require_utc
from .contracts import (
    AgentResponse,
    AgentStructuredFinding,
    ToolCallRecord,
)


class AgentParsingError(Exception):
    """Raised when LLM output cannot be parsed into a valid AgentResponse."""


def extract_json_payload(raw_text: str) -> Dict[str, Any]:
    """Extract JSON from raw LLM text, stripping markdown code blocks if present."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise AgentParsingError("Model returned empty text response")

    text = raw_text.strip()

    # Match ```json ... ``` or ``` ... ```
    fence_pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
    match = re.search(fence_pattern, text)
    if match:
        json_str = match.group(1).strip()
    else:
        # Check if first '{' and last '}' exist
        start_idx = text.find("{")
        end_idx = text.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = text[start_idx : end_idx + 1]
        else:
            json_str = text

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise AgentParsingError(f"Failed to decode JSON from model response: {e}") from e

    if not isinstance(data, dict):
        raise AgentParsingError(f"Expected JSON object, got {type(data).__name__}")

    return data


def parse_agent_response(
    raw_text: str,
    incident_id: str,
    tool_calls_used: Sequence[ToolCallRecord],
    model_id: str,
    prompt_version: str,
    iterations_count: int,
    now: Optional[datetime] = None,
    db: Optional[Any] = None,
) -> AgentResponse:
    """Parse raw model output into a strongly typed AgentResponse.

    Args:
        raw_text: Raw string returned by the model.
        incident_id: Incident under investigation.
        tool_calls_used: Sequence of tool execution records.
        model_id: Model identifier used.
        prompt_version: Version of prompt template used.
        iterations_count: Number of loop iterations executed.
        now: Timestamp of the response (aware UTC).
        db: Optional database repository for evidence resolution.

    Returns:
        A validated AgentResponse instance.
    """
    when = require_utc(now) if now is not None else datetime.now().astimezone()
    data = extract_json_payload(raw_text)

    reasoning = data.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise AgentParsingError("Missing or empty 'reasoning' in agent response")

    verified_facts_raw = data.get("verified_facts", [])
    if not isinstance(verified_facts_raw, list):
        raise AgentParsingError("'verified_facts' must be a list of strings")
    verified_facts = tuple(str(f) for f in verified_facts_raw)

    uncertainty_raw = data.get("uncertainty_or_limitations", [])
    if not isinstance(uncertainty_raw, list):
        raise AgentParsingError("'uncertainty_or_limitations' must be a list of strings")
    uncertainty = tuple(str(u) for u in uncertainty_raw)

    # Parse findings
    findings_raw = data.get("findings", [])
    if not isinstance(findings_raw, list):
        raise AgentParsingError("'findings' must be a list of objects")

    findings: List[AgentStructuredFinding] = []
    for idx, f in enumerate(findings_raw):
        if not isinstance(f, dict):
            raise AgentParsingError(f"findings[{idx}] must be a dict")
        findings.append(
            AgentStructuredFinding(
                title=str(f.get("title", f"Finding {idx + 1}")),
                dimension=str(f["dimension"]) if f.get("dimension") else None,
                observed_value=str(f["observed_value"]) if f.get("observed_value") else None,
                evidence_ref=str(f["evidence_ref"]) if f.get("evidence_ref") else None,
                summary=str(f.get("summary", "")),
            )
        )

    # Parse proposed intent if present
    intent_obj: Optional[AgentIntent] = None
    intent_data = data.get("proposed_intent")

    if intent_data is not None and isinstance(intent_data, dict) and intent_data.get("action"):
        action_str = str(intent_data["action"]).lower()
        try:
            action_enum = IntentAction(action_str)
        except ValueError as e:
            raise AgentParsingError(f"Invalid IntentAction: {action_str!r}") from e

        intent_reason = str(intent_data.get("reason", ""))
        if len(intent_reason.strip()) < 20:
            raise AgentParsingError(
                f"AgentIntent reason too short ({len(intent_reason.strip())} < 20 chars). "
                "Must provide substantive explanation."
            )

        # Parse target
        target_obj: Optional[IntentTarget] = None
        target_type_str = intent_data.get("target_type")
        target_id_str = intent_data.get("target_id")

        if target_type_str and target_id_str:
            try:
                target_type = TargetEntityType(str(target_type_str).lower())
            except ValueError as e:
                raise AgentParsingError(f"Invalid TargetEntityType: {target_type_str!r}") from e
            target_obj = IntentTarget(entity_type=target_type, entity_id=str(target_id_str).strip())
        elif action_enum not in (IntentAction.NO_ACTION, IntentAction.ESCALATE_TO_HUMAN):
            # Target is required for non-targetless actions
            target_obj = IntentTarget(entity_type=TargetEntityType.INCIDENT, entity_id=incident_id)

        evidence_refs_raw = intent_data.get("evidence_refs", [])
        if not isinstance(evidence_refs_raw, list):
            raise AgentParsingError("'evidence_refs' must be a list of strings")
        evidence_refs = tuple(
            str(ref).strip() for ref in evidence_refs_raw
            if ref and str(ref).strip() not in ("ev_...", "ev_…", "null")
        )

        # If action requires evidence but none was provided, look for real evidence_refs in findings or db
        if action_enum != IntentAction.NO_ACTION and not evidence_refs:
            found_refs = [
                f.evidence_ref for f in findings
                if f.evidence_ref and f.evidence_ref not in ("ev_...", "ev_…", "null")
            ]
            if not found_refs and db is not None:
                inc = db.get_incident(incident_id)
                if inc and inc.evidence:
                    found_refs.extend(ev.evidence_id for ev in inc.evidence)
                inv = db.get_investigation(incident_id)
                if inv and inv.evidence:
                    found_refs.extend(ev.evidence_id for ev in inv.evidence)
            evidence_refs = tuple(set(found_refs))

        # Claimed amount
        claimed_amount: Optional[Money] = None
        if intent_data.get("claimed_amount_paise") is not None:
            claimed_paise = int(intent_data["claimed_amount_paise"])
            claimed_amount = Money(claimed_paise, Currency.INR) if claimed_paise > 0 else None

        # Confidence
        confidence_val: Optional[Decimal] = None
        if intent_data.get("confidence") is not None:
            try:
                confidence_val = Decimal(str(intent_data["confidence"]))
            except Exception:
                confidence_val = Decimal("0.90")

        # Generate deterministic intent_id
        intent_id = f"intent_{short_digest({'incident_id': incident_id, 'action': action_enum.value, 'when': when.isoformat()})}"

        raw_params = intent_data.get("parameters", {})
        cleaned_params = {}
        if isinstance(raw_params, dict):
            for k, v in raw_params.items():
                if not isinstance(k, str):
                    continue
                clean_k = k.lower().replace("-", "_")
                if isinstance(v, bool):
                    cleaned_params[clean_k] = v
                elif isinstance(v, int):
                    cleaned_params[clean_k] = v
                elif isinstance(v, float):
                    cleaned_params[clean_k] = str(v)
                elif isinstance(v, (str, Money)):
                    cleaned_params[clean_k] = v
                else:
                    cleaned_params[clean_k] = str(v)

        try:
            intent_obj = AgentIntent(
                intent_id=intent_id,
                incident_id=incident_id,
                action=action_enum,
                reason=intent_reason,
                proposed_at=when,
                model_id=model_id,
                prompt_version=prompt_version,
                target=target_obj,
                parameters=cleaned_params,
                evidence_refs=evidence_refs,
                claimed_amount=claimed_amount,
                confidence=confidence_val,
            )
        except DomainValidationError as e:
            raise AgentParsingError(f"AgentIntent domain validation failed: {e}") from e

    return AgentResponse(
        incident_id=incident_id,
        reasoning=reasoning,
        verified_facts=verified_facts,
        findings=tuple(findings),
        uncertainty_or_limitations=uncertainty,
        tool_calls_used=tuple(tool_calls_used),
        proposed_intent=intent_obj,
        model_id=model_id,
        prompt_version=prompt_version,
        iterations_count=iterations_count,
        raw_model_response=raw_text,
    )
