# Merchant FinPilot — AI Financial Autopilot for Merchants

Merchant FinPilot is an autonomous, fail-closed financial incident intelligence and remediation platform for Razorpay merchants.

---

## Architectural Principles

1. **"LLM proposes. Deterministic systems verify. Policy decides. Execution executes."**
2. **"The orchestrator coordinates. It does NOT duplicate business logic."**
3. **"The API exposes the application. It does NOT duplicate application logic."**

```
HTTP Request
     │
     ▼
┌────────────────────────────────────────────────────────┐
│ backend/api/ (FinPilotApp WSGI Callable)               │
│ - Request validation & response serialization          │
└────────────────────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────────────────────┐
│ backend/application/ (FinancialIncidentOrchestrator)   │
│ - Stage gate management & fail-closed lifecycle        │
└────────────────────────────────────────────────────────┘
     │
     ├──> Stage 1: Detection (backend/detection/)
     ├──> Stage 2: Investigation (backend/investigation/)
     ├──> Stage 3: Reasoning Agent (backend/agent/)
     ├──> Stage 4: Financial Verifier (backend/verification/)
     ├──> Stage 5: Policy Engine (backend/policy/)
     └──> Stage 6: Execution Engine (backend/execution/)
```

---

## Quick Start (Local Development)

The application runs using Python's standard library alone (`wsgiref`) without requiring external web frameworks.

### 1. Run in Offline / Mock Mode (Default — No API Key Required)

```bash
python3 -m backend.server --host 127.0.0.1 --port 8000 --mode mock
```

Or simply:
```bash
python3 backend/server.py
```

### 2. Run with Google Gemini (Real Mode)

Set your Gemini API key in the environment:

```bash
export GEMINI_API_KEY="your-gemini-api-key"
export GEMINI_MODEL="gemini-2.5-flash"  # Optional, default: gemini-2.5-flash
export FINPILOT_MODE="real"
python3 -m backend.server --host 127.0.0.1 --port 8000 --mode real
```

> **IMPORTANT SAFETY NOTICE**:
> - Running with a real Gemini API key provides live LLM reasoning for root cause investigation.
> - Real Gemini reasoning **DOES NOT** perform real financial execution.
> - Gemini-generated proposals are strictly un-executed intents (`AgentIntent`).
> - Every proposal must pass pure deterministic verification (`FinancialVerifier`) and policy authorization (`PolicyEngine`).
> - Outbound execution is isolated by `SimulatedExecutionAdapter` and explicitly marked as `is_simulation: true`.


---

## API Endpoints

### 1. Health Check
```bash
curl -X GET http://127.0.0.1:8000/api/v1/health
```

### 2. Process Incident via Synthetic Scenario
```bash
curl -X POST http://127.0.0.1:8000/api/v1/incidents/process \
  -H "Content-Type: application/json" \
  -d '{
    "merchant_id": "merchant_live_01",
    "scenario_id": "upi_failure_spike"
  }'
```

### 3. Fetch Incident Record
```bash
curl -X GET http://127.0.0.1:8000/api/v1/incidents/inc_12345
```

### 4. Fetch Cryptographically Verified Audit Trail
```bash
curl -X GET http://127.0.0.1:8000/api/v1/audit
```

---

## Running Tests

### 1. API Unit Tests
```bash
python3 -m unittest backend.tests.api.test_api -v
```

### 2. API HTTP Integration Tests
```bash
python3 -m unittest backend.tests.api.test_integration_http -v
```

### 3. Complete Project Test Suite (All 555 tests)
```bash
python3 -m unittest discover -s backend/tests -t . -v
```

---

## Security & Invariants

- **Exact Integer Minor Units**: All monetary values are strictly tracked in integer paise (`Money(minor_units, Currency.INR)`). No JSON floating-point money values exist.
- **Zero Secrets Leaked**: API keys, credentials, and authorization headers are never logged, stored in audit payloads, or returned in HTTP responses.
- **Fail-Closed Execution**: Execution is strictly gated behind deterministic verification (`FinancialVerifier`) and policy authorization (`PolicyEngine`). The API layer cannot bypass validation or trigger direct execution.
- **Simulation Protection**: In test/local mode, execution is handled by `SimulatedExecutionAdapter` and explicitly marked with `is_simulation: true`.

---

## Razorpay TEST Integration & Webhook Reconciliation

### 1. Architectural Model & Guarantees
FinPilot supports live integration with Razorpay's **TEST API** while preserving strict deterministic safety gates:
```
Razorpay TEST Webhook (payment.failed)
  ↓
Ingestion & Normalization (PaymentNormalizer → PaymentEnricher → SQLite DB)
  ↓
DETECT (Window Evaluation & Incident Spike Detection)
  ↓
INVESTIGATE (Multi-Dimensional Breakdown & InvestigationReport)
  ↓
Gemini REASON (Gemini 3.1 Flash Lite Autonomous Investigation → AgentIntent)
  ↓
VERIFY (12/12 Deterministic Financial Invariants → VerifiedIntent)
  ↓
AUTHORIZE (10/10 Deterministic Policy Rules → PolicyDecision ALLOW)
  ↓
EXECUTE (ExecutionEngine Idempotency & Pre-Execution Audit)
  ↓
Razorpay TEST API (POST /v1/payment_links → Live Receipt with plink_...)
  ↓
Asynchronous Webhook (payment_link.paid with HMAC-SHA256 Signature)
  ↓
Reconciliation (Correlation with plink_... → In-Place Update → OUTCOME_VERIFIED Audit)
```

### 2. Environment Configuration
The following variables govern the Razorpay TEST mode and are loaded strictly from the environment:
- `RAZORPAY_KEY_ID`: Must start with `rzp_test_` (e.g. `rzp_test_TWIqJasTUg8Ni0`).
- `RAZORPAY_KEY_SECRET`: Secret key for Basic Authentication.
- `RAZORPAY_WEBHOOK_SECRET`: Secret configured in Razorpay Dashboard for HMAC-SHA256 signature verification.
- `FINPILOT_EXECUTION_MODE`: Set to `razorpay_test` to dispatch real API calls to Razorpay TEST API. Defaults safely to `simulated`.

### 3. Safety Guardrails & Live Key Blocking
- **`rzp_live_*` Strictly Forbidden**: `RazorpayExecutionAdapter` inspects `key_id.startswith("rzp_test_")`. Any key starting with `rzp_live_` fails immediately with `LIVE_MODE_FORBIDDEN` and makes zero external network calls.
- **Webhook Ingestion Isolation**: Webhooks only persist facts/telemetry into the database and reconcile existing executions. Webhook ingestion **never invokes Gemini** and **never triggers financial executions**.
- **Deterministic Gates Required**: Gemini cannot call Razorpay directly. Outbound execution only dispatches after `FinancialVerifier` (12/12 checks) and `PolicyEngine` (10/10 rules) produce an authorized decision.

### 4. Configuring Webhooks in Razorpay Dashboard
1. Webhook URL: `https://<your-render-app>.onrender.com/api/v1/webhooks/razorpay`
2. Secret: Generate a secure secret and set it in `RAZORPAY_WEBHOOK_SECRET`.
3. Subscribed Events (minimal recommended set):
   - `payment_link.paid`
   - `payment_link.cancelled`
   - `payment_link.expired`
   - `payment.captured`
   - `payment.failed`
   - `payment.authorized`

### 5. Signature Verification & Replay Protection
- Signature is verified using constant-time `hmac.compare_digest` computed over raw request body bytes and `X-Razorpay-Signature`.
- Tampered payloads or missing secrets fail with HTTP 401 before any database or execution mutation.
- Duplicate webhook deliveries are deduplicated by `event_id` and return HTTP 200 `duplicate_skipped`.

### 6. Execution Correlation & In-Place Reconciliation
The reconciler (`RazorpayReconciler`) resolves outbound executions using a 4-tier deterministic lookup:
1. `plink_id`: Provider reference (`plink_...`).
2. `reference_id`: Outbound idempotency key reference (`reference_id=idempotency_key[:40]`).
3. `notes.idempotency_key`: Full idempotency key embedded in metadata notes.
4. `notes.intent_id`: Intent ID embedded in metadata notes.

When reconciled:
- Execution status is updated in-place (no duplicate executions created).
- An immutable `AuditEventType.OUTCOME_VERIFIED` event is appended to the audit ledger.
- If currency or action mismatches, it is flagged as `ReconciliationStatus.MISMATCH` and escalated (`AuditEventType.ESCALATED`).

### 7. Running Verification Tests
```bash
# Run Razorpay Unit, Safety & E2E Test Suite (55 tests)
python3 -m unittest discover -s backend/tests/razorpay -t . -p "test_*.py"

# Run Live Test against Razorpay TEST API (Requires .env with rzp_test_ keys)
PYTHONPATH=. python3 backend/tests/razorpay/verify_live_razorpay_test_flow.py

# Run Full Backend Test Suite (681 tests)
python3 -m unittest discover -s backend/tests -t . -p "test_*.py"

# Run Canonical Scenarios (37 tests)
python3 -m unittest discover -s backend/tests/data -t . -p "test_*.py"

# Run Frontend Production Build
cd frontend && npm run build
```

