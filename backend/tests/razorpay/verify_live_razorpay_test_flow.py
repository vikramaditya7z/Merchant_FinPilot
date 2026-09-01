"""Direct live test against Razorpay TEST servers using credentials from .env.

Runs:
1. Loads credentials from .env (RAZORPAY_KEY_ID=rzp_test_..., RAZORPAY_KEY_SECRET=...).
2. Verifies Razorpay TEST credentials by calling Razorpay API (GET /v1/payments?count=1).
3. Executes authorized action through RazorpayExecutionAdapter in TEST mode (POST /v1/payment_links).
4. Confirms real Razorpay test payment link created (plink_...).
5. Delivers corresponding HMAC-signed webhook to RazorpayService.
6. Confirms reconciliation updates the execution status in ExecutionStore to SUCCEEDED.
7. Confirms AuditLog records AuditEventType.OUTCOME_VERIFIED.
"""

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime

from backend.audit.store import AuditLog
from backend.db.database import Database
from backend.domain.enums import (
    AuditEventType,
    Currency,
    ExecutionStatus,
    IntentAction,
    PolicyVerdict,
    TargetEntityType,
)
from backend.domain.intent import AgentIntent, IntentTarget
from backend.domain.money import Money
from backend.domain.policy import PolicyDecision
from backend.domain.window import UTC
from backend.execution.adapters import RazorpayExecutionAdapter
from backend.execution.contracts import ExecutionRequest
from backend.execution.engine import ExecutionEngine
from backend.execution.store import ExecutionStore
from backend.razorpay.client import RazorpayClient
from backend.razorpay.config import RazorpayConfig
from backend.razorpay.reconciler import RazorpayReconciler
from backend.razorpay.service import RazorpayService
from backend.server import load_env_file


def run_live_test():
    load_env_file(".env")
    config = RazorpayConfig.from_env()
    print("[1] Loaded Razorpay Configuration:")
    print(f"    Key ID: {config.key_id}")
    print(f"    Configured: {config.is_configured}")

    if not config.is_configured:
        print("ERROR: Razorpay credentials missing in .env")
        sys.exit(1)

    if not config.key_id.startswith("rzp_test_"):
        print("ERROR: Safety violation - only rzp_test_ keys permitted.")
        sys.exit(1)

    # Use existing or temporary webhook secret for HMAC signing test
    webhook_secret = config.webhook_secret or "whsec_live_test_secret_12345"
    test_config = RazorpayConfig(
        key_id=config.key_id,
        key_secret=config.key_secret,
        webhook_secret=webhook_secret,
        api_base_url=config.api_base_url,
    )

    client = RazorpayClient(config=test_config)

    # 1. Test Read Connectivity
    print("\n[2] Testing Razorpay TEST API Read Connectivity...")
    payments_resp = client.fetch_payments(count=1)
    print(f"    GET /v1/payments succeeded. Items returned: {len(payments_resp.get('items', []))}")

    # 2. Setup FinPilot Execution & Reconciliation Core
    db = Database(":memory:")
    audit_log = AuditLog()
    store = ExecutionStore()
    reconciler = RazorpayReconciler(store=store, audit_log=audit_log)
    adapter = RazorpayExecutionAdapter(client=client, config=test_config)
    engine = ExecutionEngine(adapter=adapter, store=store, audit_log=audit_log)
    service = RazorpayService(
        config=test_config,
        client=client,
        execution_store=store,
        reconciler=reconciler,
        database=db,
        audit_log=audit_log,
    )

    now = datetime.now().astimezone()
    ts = int(now.timestamp())
    intent = AgentIntent(
        intent_id=f"intent_live_{ts}",
        incident_id=f"inc_live_{ts}",
        action=IntentAction.CREATE_PAYMENT_LINK,
        reason="Automated live end-to-end test payment link generation",
        proposed_at=now,
        model_id="gemini-3.1-flash-lite-preview",
        prompt_version="v2.0",
        target=IntentTarget(entity_type=TargetEntityType.PAYMENT, entity_id="pay_failed_sample_01"),
        parameters={"amount": 50000, "currency": "INR"},
        evidence_refs=("ev_live_01",),
        claimed_amount=Money(50000, Currency.INR),
    )

    decision = PolicyDecision(
        decision_id=f"dec_live_{ts}",
        intent_id=intent.intent_id,
        intent_hash=intent.content_hash(),
        verdict=PolicyVerdict.ALLOW,
        rationale="Policy allowed test execution against Razorpay TEST API",
        evaluated_at=now,
        expires_at=PolicyDecision.default_expiry(now, 300),
        rule_set_version="pol-v1",
    )

    # 3. Execute Outbound Creation on Razorpay TEST
    print("\n[3] Executing Outbound Payment Link on Razorpay TEST API...")
    exec_result = engine.execute(decision=decision, intent=intent, now=now)

    print(f"    Execution Status: {exec_result.status.value}")
    print(f"    Provider Reference: {exec_result.provider_reference}")
    print(f"    Execution Message: {exec_result.message}")
    print(f"    Error Code: {exec_result.error_code}")
    print(f"    Error Message: {exec_result.error_message}")
    print(f"    Idempotency Key: {exec_result.idempotency_key}")

    assert exec_result.status == ExecutionStatus.SUCCEEDED, f"Expected SUCCEEDED, got {exec_result.status}"
    assert exec_result.provider_reference and exec_result.provider_reference.startswith("plink_"), "Expected plink_ reference"
    plink_id = exec_result.provider_reference

    # 4. Inbound Webhook Reconciliation
    print(f"\n[4] Delivering Simulated Razorpay Webhook for {plink_id}...")
    webhook_payload = {
        "entity": "event",
        "account_id": "acc_live_test_merchant",
        "event": "payment_link.paid",
        "contains": ["payment_link", "payment"],
        "payload": {
            "payment_link": {
                "entity": {
                    "id": plink_id,
                    "amount": 50000,
                    "amount_paid": 50000,
                    "status": "paid",
                    "currency": "INR",
                    "reference_id": exec_result.idempotency_key[:40],
                    "notes": {
                        "incident_id": intent.incident_id,
                        "intent_id": intent.intent_id,
                        "idempotency_key": exec_result.idempotency_key,
                    },
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_live_test_receipt_001",
                    "amount": 50000,
                    "currency": "INR",
                    "status": "captured",
                    "method": "upi",
                    "created_at": int(now.timestamp()),
                }
            },
        },
    }

    raw_body = json.dumps(webhook_payload).encode("utf-8")
    sig = hmac.new(webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    status_code, resp = service.handle_webhook(raw_body, sig)
    print(f"    Webhook Status Code: {status_code}")
    print(f"    Webhook Response: {resp}")

    assert status_code == 200, f"Expected 200, got {status_code}"
    assert resp.get("reconciliation", {}).get("status") == "matched", "Reconciliation did not match"
    assert resp.get("reconciliation", {}).get("reconciled_status") == "succeeded", "Expected reconciled_status succeeded"

    # 5. Verify Store Update
    updated_exec = store.get(exec_result.idempotency_key)
    print(f"\n[5] Verified Reconciled Execution State:")
    print(f"    Execution ID: {updated_exec.execution_id}")
    print(f"    Status: {updated_exec.status.value}")
    print(f"    Message: {updated_exec.message}")
    assert updated_exec.status == ExecutionStatus.SUCCEEDED
    assert "verified PAID" in updated_exec.message

    # 6. Verify Audit Trail
    outcome_events = [e for e in audit_log.events if e.event_type == AuditEventType.OUTCOME_VERIFIED]
    print(f"\n[6] Verified Audit Trail:")
    print(f"    Total Audit Events: {len(audit_log.events)}")
    print(f"    OUTCOME_VERIFIED events: {len(outcome_events)}")
    assert len(outcome_events) == 1, "Expected exactly 1 OUTCOME_VERIFIED audit event"
    print(f"    Event Summary: {outcome_events[0].summary}")

    print("\n>>> ALL RAZORPAY TEST LIVE END-TO-END CHECKS PASSED! <<<")


if __name__ == "__main__":
    run_live_test()
