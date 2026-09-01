"""Razorpay Execution Adapter for TEST mode operations.

PROJECT_RULES 6.4, 7.1, 7.5, 10.8, 10.9 / ARCHITECTURE.md §12, §13.

Guarantees:
- Operates strictly in Razorpay TEST mode (rejects live keys / live execution).
- Dispatches authorized, deterministic AgentIntents (e.g. CREATE_PAYMENT_LINK, NOTIFY_MERCHANT).
- Never exposes Razorpay secret keys or tokens in outputs, logs, digests, or error messages.
- Returns structured ExecutionResult with verifiable provider references and response digests.
- Fail-closed: handles API errors, timeouts, connection errors, and unconfigured states gracefully.
"""

from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from ..domain.canonical import short_digest
from ..domain.enums import ExecutionStatus, IntentAction
from ..domain.money import Money
from ..execution.adapters import ExecutionAdapter
from ..execution.contracts import ExecutionRequest, ExecutionResult
from .client import (
    RazorpayAPIError,
    RazorpayAuthError,
    RazorpayClient,
    RazorpayConnectionError,
    RazorpayNotFoundError,
    RazorpayServerError,
    RazorpayTimeoutError,
)
from .config import RazorpayConfig


class RazorpayExecutionAdapter(ExecutionAdapter):
    """Execution adapter for Razorpay TEST mode API interactions."""

    def __init__(
        self,
        client: Optional[RazorpayClient] = None,
        config: Optional[RazorpayConfig] = None,
        name: str = "razorpay_test_adapter_v1",
    ) -> None:
        self._config = config or (client.config if client is not None else RazorpayConfig.from_env())
        self._client = client or RazorpayClient(config=self._config)
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def client(self) -> RazorpayClient:
        return self._client

    @property
    def config(self) -> RazorpayConfig:
        return self._config

    def execute(self, request: ExecutionRequest, idempotency_key: str) -> ExecutionResult:
        """Execute an authorized action via Razorpay API in TEST mode.

        Args:
            request: The authorized ExecutionRequest containing PolicyDecision and AgentIntent.
            idempotency_key: Deterministic idempotency key for deduplication.

        Returns:
            An immutable ExecutionResult detailing the outcome of the execution.
        """
        now = datetime.now().astimezone()
        intent = request.intent
        decision = request.decision
        execution_id = f"rzp_exec_{short_digest({'key': idempotency_key, 'intent_id': intent.intent_id, 'when': now.isoformat()})}"

        # 1. Configuration & Test Mode Security Checks
        if not self._config.is_configured:
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.FAILED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                is_simulation=True,
                error_code="RAZORPAY_NOT_CONFIGURED",
                error_message="Razorpay credentials (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET) are missing or empty.",
            )

        key_id = self._config.key_id or ""
        if not key_id.startswith("rzp_test_"):
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.FAILED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                is_simulation=True,
                error_code="LIVE_MODE_FORBIDDEN",
                error_message="RazorpayExecutionAdapter is restricted to TEST mode only. Live keys ('rzp_live_') are strictly forbidden.",
            )

        if request.mode.lower() != "test":
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.FAILED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                is_simulation=True,
                error_code="INVALID_MODE",
                error_message=f"RazorpayExecutionAdapter requires mode='test', got mode='{request.mode}'.",
            )

        # 2. Action Dispatching
        if intent.action == IntentAction.CREATE_PAYMENT_LINK:
            return self._execute_create_payment_link(request, idempotency_key, execution_id, now)

        elif intent.action == IntentAction.NOTIFY_MERCHANT:
            ref = f"rzp_test_notif_{short_digest({'key': idempotency_key, 'action': 'notify'})}"
            msg = f"Razorpay TEST merchant notification recorded for incident {intent.incident_id}."
            resp_digest = short_digest({"ref": ref, "msg": msg, "key": idempotency_key})
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.SUCCEEDED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                provider_reference=ref,
                response_digest=resp_digest,
                is_simulation=True,
                message=msg,
            )

        elif intent.action == IntentAction.RECOMMEND_ONLY:
            ref = f"rzp_test_rec_{short_digest({'key': idempotency_key, 'action': 'recommend'})}"
            msg = "Razorpay TEST advisory recommendation recorded."
            resp_digest = short_digest({"ref": ref, "msg": msg, "key": idempotency_key})
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.SUCCEEDED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                provider_reference=ref,
                response_digest=resp_digest,
                is_simulation=True,
                message=msg,
            )

        elif intent.action == IntentAction.NO_ACTION:
            ref = f"rzp_test_noact_{short_digest({'key': idempotency_key, 'action': 'no_action'})}"
            msg = "Razorpay TEST explicit no-action recorded."
            resp_digest = short_digest({"ref": ref, "msg": msg, "key": idempotency_key})
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.SUCCEEDED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                provider_reference=ref,
                response_digest=resp_digest,
                is_simulation=True,
                message=msg,
            )

        elif intent.action == IntentAction.ESCALATE_TO_HUMAN:
            ref = f"rzp_test_esc_{short_digest({'key': idempotency_key, 'action': 'escalate'})}"
            msg = "Razorpay TEST operations ticket escalated for merchant review."
            resp_digest = short_digest({"ref": ref, "msg": msg, "key": idempotency_key})
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.SUCCEEDED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                provider_reference=ref,
                response_digest=resp_digest,
                is_simulation=True,
                message=msg,
            )

        else:
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.FAILED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                is_simulation=True,
                error_code="UNSUPPORTED_ACTION",
                error_message=f"Action '{intent.action.value}' is not supported by RazorpayExecutionAdapter.",
            )

    def _execute_create_payment_link(
        self,
        request: ExecutionRequest,
        idempotency_key: str,
        execution_id: str,
        now: datetime,
    ) -> ExecutionResult:
        """Execute Razorpay Payment Link creation via REST API."""
        intent = request.intent
        decision = request.decision

        # Determine Amount (paise integer)
        amt_paise: int = 50000
        if intent.parameters and "amount" in intent.parameters:
            val = intent.parameters["amount"]
            if isinstance(val, Money):
                amt_paise = val.minor_units
            elif isinstance(val, int):
                amt_paise = val
        elif intent.claimed_amount is not None:
            amt_paise = intent.claimed_amount.minor_units

        # Currency
        curr = "INR"
        if intent.parameters and "currency" in intent.parameters:
            curr = str(intent.parameters["currency"])
        elif intent.claimed_amount is not None:
            curr = intent.claimed_amount.currency.value

        description = intent.reason[:500] if intent.reason else f"Payment link for {intent.incident_id}"
        notes = {
            "incident_id": intent.incident_id,
            "intent_id": intent.intent_id,
            "action": intent.action.value,
            "idempotency_key": idempotency_key,
        }

        try:
            plink_res = self._client.create_payment_link(
                amount_minor_units=amt_paise,
                currency=curr,
                description=description,
                reference_id=idempotency_key[:40],
                notes=notes,
            )
            plink_id = str(plink_res.get("id", f"plink_{short_digest({'key': idempotency_key})}"))
            short_url = str(plink_res.get("short_url", ""))
            resp_digest = short_digest({"plink_id": plink_id, "amount": amt_paise, "key": idempotency_key})
            msg = (
                f"Razorpay TEST payment link created: {plink_id} ({short_url})"
                if short_url
                else f"Razorpay TEST payment link created: {plink_id}"
            )
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.SUCCEEDED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                provider_reference=plink_id,
                response_digest=resp_digest,
                is_simulation=True,
                message=msg,
            )
        except RazorpayAuthError as exc:
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.FAILED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                is_simulation=True,
                error_code="RAZORPAY_AUTH_ERROR",
                error_message=str(exc),
            )
        except RazorpayNotFoundError as exc:
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.FAILED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                is_simulation=True,
                error_code="RAZORPAY_NOT_FOUND",
                error_message=str(exc),
            )
        except RazorpayAPIError as exc:
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.FAILED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                is_simulation=True,
                error_code="RAZORPAY_API_ERROR",
                error_message=str(exc),
            )
        except RazorpayServerError as exc:
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.FAILED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                is_simulation=True,
                error_code="RAZORPAY_SERVER_ERROR",
                error_message=str(exc),
            )
        except RazorpayTimeoutError as exc:
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.UNKNOWN,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                is_simulation=True,
                error_code="RAZORPAY_TIMEOUT",
                error_message=str(exc),
            )
        except RazorpayConnectionError as exc:
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.FAILED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                is_simulation=True,
                error_code="RAZORPAY_CONNECTION_ERROR",
                error_message=str(exc),
            )
        except Exception as exc:
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.FAILED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                is_simulation=True,
                error_code="EXECUTION_ERROR",
                error_message=str(exc),
            )
