"""The Core Financial Reasoning Agent for Merchant FinPilot.

PROJECT_RULES 1.2, 1.6, 2.7, 10.8 / ARCHITECTURE.md §8, §9.

Security & Architectural Invariants:
- "LLMs reason. Deterministic systems verify."
- Tool surface is the sole security boundary; zero direct DB, SQL, or execution access.
- Every financial number comes from deterministic tools.
- Agent outputs a structured AgentResponse and an un-executed AgentIntent proposal.
- Complete audit trail of all agent actions and tool invocations.
"""

import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from ..audit.store import AuditLog
from ..domain.canonical import canonical_json, digest
from ..domain.enums import AuditActor, AuditEventType
from ..domain.errors import DomainValidationError
from ..domain.window import require_utc
from ..tools.contracts import ToolResult
from ..tools.registry import BoundToolRegistry, ToolRegistry
from .contracts import (
    AgentResponse,
    LLMMessage,
    ToolCallRecord,
    ToolCallRequest,
)
from .parser import AgentParsingError, parse_agent_response
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_incident_prompt
from .provider import LLMProvider


def _sanitize_canonical(obj: Any) -> Any:
    """Recursively convert float values to exact strings so canonical_json / digest succeed."""
    if isinstance(obj, float):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _sanitize_canonical(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_canonical(x) for x in obj]
    return obj


class FinancialAgent:
    """Core LLM Reasoning Agent for incident investigation and intent proposal."""

    def __init__(
        self,
        provider: LLMProvider,
        tools: Union[BoundToolRegistry, ToolRegistry],
        max_iterations: int = 10,
        model_id: Optional[str] = None,
        prompt_version: str = PROMPT_VERSION,
        audit_log: Optional[AuditLog] = None,
    ) -> None:
        if not isinstance(provider, LLMProvider):
            raise DomainValidationError("FinancialAgent requires an LLMProvider")
        if not isinstance(tools, (BoundToolRegistry, ToolRegistry)):
            raise DomainValidationError(
                "FinancialAgent requires a ToolRegistry or BoundToolRegistry"
            )

        self._provider = provider
        self._tools = tools
        self._max_iterations = max_iterations
        self._model_id = model_id or provider.model_id
        self._prompt_version = prompt_version
        self._audit_log = audit_log

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def prompt_version(self) -> str:
        return self._prompt_version

    def investigate(
        self,
        incident_id: str,
        db: Optional[Any] = None,
        context_notes: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> AgentResponse:
        """Run the bounded investigation loop over the incident.

        Args:
            incident_id: Incident identifier to investigate.
            db: Optional Database instance (required if ToolRegistry is unbound).
            context_notes: Optional contextual notes or merchant metadata.
            now: Current timestamp injection (aware UTC).

        Returns:
            A strongly typed AgentResponse with findings and intent proposal.
        """
        if not isinstance(incident_id, str) or not incident_id.strip():
            raise DomainValidationError("incident_id must be a non-empty string")

        when = require_utc(now) if now is not None else datetime.now().astimezone()

        # Bind tool registry if necessary
        if isinstance(self._tools, ToolRegistry):
            if db is None:
                raise DomainValidationError(
                    "Database instance must be provided when tools is an unbound ToolRegistry"
                )
            bound_tools: BoundToolRegistry = self._tools.bind(db)
        else:
            bound_tools = self._tools

        # Audit agent invocation
        if self._audit_log is not None:
            self._audit_log.append(
                actor=AuditActor.AGENT,
                event_type=AuditEventType.INVESTIGATION_STARTED,
                summary=f"Agent started investigation for incident {incident_id}",
                incident_id=incident_id,
                occurred_at=when,
                payload={
                    "incident_id": incident_id,
                    "model_id": self._model_id,
                    "prompt_version": self._prompt_version,
                },
            )

        # Initialize conversation
        history: List[LLMMessage] = [
            LLMMessage(role="system", content=SYSTEM_PROMPT),
            LLMMessage(
                role="user", content=build_incident_prompt(incident_id, context_notes)
            ),
        ]

        tool_schemas = bound_tools.get_schemas()
        tool_records: List[ToolCallRecord] = []
        last_model_text: Optional[str] = None

        for iteration in range(1, self._max_iterations + 1):
            turn = self._provider.generate_turn(
                messages=history,
                tool_schemas=tool_schemas,
                temperature=0.0,
            )
            history.append(turn)

            if turn.tool_calls:
                # Process tool calls requested by the model
                for tc in turn.tool_calls:
                    res: ToolResult = bound_tools.execute(tc.tool_name, tc.arguments)
                    raw_dict = res.to_dict()
                    res_dict = _sanitize_canonical(raw_dict)
                    res_digest = digest(res_dict)
                    sanitized_args = _sanitize_canonical(tc.arguments)

                    record = ToolCallRecord(
                        call_id=tc.call_id,
                        tool_name=tc.tool_name,
                        arguments=dict(sanitized_args) if isinstance(sanitized_args, dict) else {},
                        success=res.success,
                        result_digest=res_digest,
                        raw_result=raw_dict,
                    )
                    tool_records.append(record)

                    # Audit tool call
                    if self._audit_log is not None:
                        self._audit_log.append(
                            actor=AuditActor.AGENT,
                            event_type=AuditEventType.TOOL_CALLED,
                            summary=f"Agent executed tool '{tc.tool_name}' (success={res.success})",
                            incident_id=incident_id,
                            occurred_at=when,
                            payload={
                                "tool_name": tc.tool_name,
                                "call_id": tc.call_id,
                                "arguments": sanitized_args,
                                "result_digest": res_digest,
                                "success": res.success,
                            },
                        )

                    # Append tool result to conversation history
                    history.append(
                        LLMMessage(
                            role="tool",
                            content=canonical_json(res_dict),
                            tool_call_id=tc.call_id,
                            tool_name=tc.tool_name,
                        )
                    )
                # Continue next iteration for model to process tool outputs
                continue

            # Model produced text / final answer
            last_model_text = turn.content or ""
            try:
                response = parse_agent_response(
                    raw_text=last_model_text,
                    incident_id=incident_id,
                    tool_calls_used=tool_records,
                    model_id=self._model_id,
                    prompt_version=self._prompt_version,
                    iterations_count=iteration,
                    now=when,
                    db=getattr(bound_tools, "db", db),
                )

                # Record audit events for completion
                if self._audit_log is not None:
                    self._audit_log.append(
                        actor=AuditActor.AGENT,
                        event_type=AuditEventType.AGENT_REASONING_RECORDED,
                        summary=f"Agent synthesized reasoning with {len(response.findings)} finding(s)",
                        incident_id=incident_id,
                        occurred_at=when,
                        payload={
                            "incident_id": incident_id,
                            "findings_count": len(response.findings),
                            "verified_facts_count": len(response.verified_facts),
                            "reasoning_digest": digest(response.reasoning),
                        },
                    )

                    if response.proposed_intent is not None:
                        self._audit_log.append(
                            actor=AuditActor.AGENT,
                            event_type=AuditEventType.INTENT_PROPOSED,
                            summary=(
                                f"Agent proposed intent {response.proposed_intent.intent_id} "
                                f"({response.proposed_intent.action.value})"
                            ),
                            incident_id=incident_id,
                            occurred_at=when,
                            payload={
                                "intent_id": response.proposed_intent.intent_id,
                                "action": response.proposed_intent.action.value,
                                "content_hash": response.proposed_intent.content_hash(),
                                "evidence_refs": list(response.proposed_intent.evidence_refs),
                            },
                        )

                return response

            except AgentParsingError as e:
                if iteration < self._max_iterations:
                    # Allow a single reprompt on malformed schema output
                    history.append(
                        LLMMessage(
                            role="user",
                            content=(
                                f"Your response did not match the required JSON schema: {e}. "
                                "Please output ONLY a single valid JSON object strictly matching the required schema."
                            ),
                        )
                    )
                    continue
                else:
                    # Maximum iterations reached with parsing error: synthesize fallback safe response
                    break

        # Fallback if iterations exhausted or persistent parsing error
        fallback_facts = tuple(
            f"Executed tool '{r.tool_name}' with status {'SUCCESS' if r.success else 'ERROR'}"
            for r in tool_records
        )
        return AgentResponse(
            incident_id=incident_id,
            reasoning=(
                "Investigation reached iteration limit or encountered formatting errors. "
                "Retrieved deterministic tool evidence is preserved; escalating for review."
            ),
            verified_facts=fallback_facts or ("No tool facts successfully retrieved.",),
            findings=(),
            uncertainty_or_limitations=(
                "Investigation concluded prematurely without structured JSON synthesis.",
            ),
            tool_calls_used=tuple(tool_records),
            proposed_intent=None,
            model_id=self._model_id,
            prompt_version=self._prompt_version,
            iterations_count=self._max_iterations,
            raw_model_response=last_model_text,
        )

    # Alias for orchestrator compatibility
    investigate_and_propose = investigate
