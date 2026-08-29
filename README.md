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
