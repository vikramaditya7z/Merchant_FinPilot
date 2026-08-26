# Merchant FinPilot — Architecture

**AI Financial Autopilot for Merchants**
Razorpay Buildathon 2026 · Track 5 (Open Track)

**Core principle: LLMs reason. Deterministic systems verify.**

---

## Document status

| | |
|---|---|
| Version | 0.2 (Day 2) |
| Last updated | 2026-08-26 |
| Applies to | 10-day MVP |
| Companion | [PROJECT_RULES.md](PROJECT_RULES.md) — binding engineering rules |

Anything below marked **`TBD`** or **`REQUIRES OFFICIAL DOC VERIFICATION`** is an
open item. It must not be implemented against until the referenced official
Razorpay documentation has been read and the finding recorded in this file.

---

## 1. Product purpose

Merchant FinPilot is an autonomous financial incident agent for merchants.

When a merchant's payment revenue degrades, FinPilot detects it, investigates
it across financial dimensions, quantifies the money at risk, reasons about the
root cause, proposes a bounded corrective action, submits that action to
deterministic authorization, executes only what is authorized, and then verifies
that the real-world financial outcome matches what was intended — leaving a
complete audit trail.

It is an **agent that closes a financial loop**, not a chatbot that answers
questions about money.

---

## 2. Problem being solved

A merchant processing thousands of payments per day experiences a failure-rate
spike: one payment method starts failing, one acquirer degrades, one region
breaks, checkout regresses at peak hour.

Today the merchant:

1. Notices late — often via customer complaints, hours in.
2. Cannot separate a real incident from normal variance.
3. Cannot quantify the loss (how many transactions, how much GMV, how much
   is recoverable).
4. Cannot localise the cause without slicing dashboards by hand.
5. Reacts slowly, and often wrongly, because the diagnosis was a guess.

The cost is **silent, continuous revenue leakage**. Every minute of an
undiagnosed degradation is unrecoverable GMV.

### Why an LLM is genuinely needed

The hard part is not arithmetic — arithmetic is easy and must be deterministic.
The hard part is **investigation under uncertainty**: deciding which dimension
to slice next, recognising that a UPI spike concentrated in one acquirer at one
hour is an acquirer incident rather than a method incident, distinguishing
correlation from cause, and choosing a proportionate response. That is
open-ended reasoning over evidence. That is what the LLM is for.

### Why the LLM cannot be trusted with the money

An LLM that computes `revenue_at_risk` will sometimes be wrong, and will always
be confident. An LLM that can call an arbitrary Razorpay endpoint can move real
money on the strength of a hallucinated premise. So the system is built so that
**the LLM's authority ends at the word "propose."**

---

## 3. What makes this different (positioning)

We deliberately chose the Open Track, and deliberately did **not** build:

| Not this | Why not |
|---|---|
| A merchant analytics chatbot | Razorpay already has merchant analytics and AI surfaces. A Q&A wrapper adds nothing. |
| `payment.failed` → create payment link | A one-step reflex, not an agent. Razorpay already has specialised recovery workflows. |
| An LLM with tool access to the payments API | Unsafe by construction. The interesting engineering is the *constraint*, not the access. |

Our contribution is the **full verified loop**:

```
autonomous financial incident investigation
  + deterministic financial reasoning over verified facts
  + independent deterministic verification of agent intent
  + policy-authorized, bounded execution
  + post-action outcome verification
  + complete audit trail
```

The differentiator is that **an LLM drives a consequential financial action and
is structurally incapable of doing so unsafely.**

---

## 4. MVP scope (10 days)

### First and only incident class: payment / revenue degradation

**In scope for the MVP:**

- Ingest Razorpay payment data / events into structured financial facts.
- Deterministic detection of failure-rate degradation against a baseline.
- Agent-driven investigation across dimensions: payment method, time,
  geography/segment, failure reason, provider/route, affected volume.
- Deterministic quantification: baseline rate, current rate, deviation,
  significance, affected transactions, affected GMV, revenue at risk.
- Agent root-cause reasoning over **verified** facts only.
- A structured `AgentIntent` proposing exactly one bounded action.
- An independent deterministic **Financial Verifier** that re-derives every
  number in the intent from source data.
- A deterministic **Policy Engine** returning `ALLOW` / `BLOCK` / `ESCALATE`.
- A dumb, policy-agnostic **execution layer** for one or two bounded,
  officially documented Razorpay actions (test mode only).
- Deterministic **post-action verification** against real Razorpay state.
- An append-only **audit trail** covering every consequential step.
- A synthetic dataset with 11 ground-truth scenarios, plus an evaluation
  harness that scores the agent against those labels.
- A thin demo UI: incident timeline, evidence, reasoning, decision, outcome.

### 4.1 Non-goals

Explicitly out of scope. Do not build these; do not partially build them.

| Non-goal | Rationale |
|---|---|
| Generic merchant analytics / chat | Not our differentiation (§3). |
| Multi-agent orchestration | One agent, well-constrained, beats a swarm. |
| LLM-as-judge in the decision path | Verification must be deterministic. A judge may score *evaluations* offline, never authorize an action. |
| RAG / vector database | No corpus problem here. Facts come from the database, not retrieval. |
| Forecasting / ML anomaly models | Baseline + deviation + significance is sufficient and explainable. |
| Live-mode money movement | Test mode only. Enforced by config guard. |
| Refunds, payouts, settlements, disputes | Different incident classes. Later. |
| Fraud / chargeback detection | Different domain. |
| Multi-currency | INR / paise only for the MVP (§7.1). |
| Multi-tenant, auth, RBAC | Single-merchant demo scope. |
| Microservices, Kubernetes, prod deploy | Modular monolith (ADR-002). |
| Real-time streaming infrastructure | Polling + webhooks suffice. |

---

## 5. The four-authority model

This is the central architectural idea. Four authorities, four disjoint
responsibilities, no overlap.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  LLM            reasoning, investigation, tool selection, hypothesis     │
│                 formation, root-cause narrative, intent proposal,        │
│                 explanation                                              │
│                                                                          │
│                 CANNOT: compute money, mutate state, call an             │
│                 unrestricted API, authorize itself                       │
├──────────────────────────────────────────────────────────────────────────┤
│  Python         financial truth. All arithmetic, all rates, all          │
│                 baselines, all deviation, all exposure, all eligibility, │
│                 all validation, all verification. Deterministic,         │
│                 pure, unit-tested.                                       │
├──────────────────────────────────────────────────────────────────────────┤
│  Razorpay       external financial state and the only place a            │
│                 consequential action actually happens. Authoritative     │
│                 for payment/order state.                                 │
├──────────────────────────────────────────────────────────────────────────┤
│  Policy         authorization. Given a verified intent, decides          │
│                 ALLOW / BLOCK / ESCALATE. Fails closed.                  │
├──────────────────────────────────────────────────────────────────────────┤
│  Audit trail    accountability. Append-only record of every fact,        │
│                 decision, action and outcome. Non-optional.              │
└──────────────────────────────────────────────────────────────────────────┘
```

**The boundary in one line:**
`LLM = reasoning/orchestration · Python = financial truth/verification ·
Razorpay = external financial state/actions · Policy = authorization ·
Audit trail = accountability.`

### 5.1 The trust rule

> No number produced by the LLM is ever used for anything except display.
> Every number that influences a decision is re-derived by Python from source
> data before the decision is made.

The `AgentIntent` may *contain* numbers. The Financial Verifier recomputes each
one from the underlying payment records and compares. A mismatch beyond a
declared tolerance is a `BLOCK`, not a correction.

---

## 6. System architecture

Modular monolith. One deployable, hard internal seams.

```
┌─────────────┐   webhooks / polling
│  Razorpay   │──────────────────────────────┐
└─────────────┘                              ▼
                                    ┌──────────────────┐
                                    │  razorpay/       │  boundary: signature
                                    │  (adapter)       │  verification, mapping
                                    └────────┬─────────┘  to domain contracts
                                             ▼
                                    ┌──────────────────┐
                                    │  db/  models/    │  structured financial
                                    │  (facts store)   │  facts — the only
                                    └────────┬─────────┘  internal truth source
                                             ▼
                                    ┌──────────────────┐
                                    │  financial/      │  DETERMINISTIC
                                    │  (engine)        │  counts · rates ·
                                    └────────┬─────────┘  baseline · deviation ·
                                             ▼            exposure
                                    ┌──────────────────┐
                                    │  detection/      │  DETERMINISTIC
                                    │                  │  opens FinancialIncident
                                    └────────┬─────────┘
                                             ▼
                    ┌────────────────────────────────────────────┐
                    │  agent/            tools/                  │
                    │  ┌──────────┐      ┌──────────────────┐   │
                    │  │   LLM    │◄────►│ narrow read-only  │   │
                    │  │ reasoning│      │ tools; every      │   │
                    │  └────┬─────┘      │ result computed   │   │
                    │       │            │ by financial/     │   │
                    │       │            └──────────────────┘   │
                    └───────┼────────────────────────────────────┘
                            ▼  AgentIntent (a proposal, not an action)
                    ┌──────────────────┐
                    │  verification/   │  Financial Verifier: re-derive every
                    │  (verifier)      │  number; validate payment/order state
                    └────────┬─────────┘
                             ▼  VerifiedIntent
                    ┌──────────────────┐
                    │  policy/         │  Policy Engine: limits, duplicates,
                    │  (authorizer)    │  eligibility, kill switch → decision
                    └────────┬─────────┘
                             │  ALLOW ──────┐   BLOCK ──► stop   ESCALATE ──► human
                             ▼              ▼
                    ┌──────────────────┐  ┌──────────────────┐
                    │  execution/      │──│  razorpay/       │  bounded action,
                    │  (dumb executor) │  │  (adapter)       │  idempotent
                    └────────┬─────────┘  └──────────────────┘
                             ▼
                    ┌──────────────────┐
                    │  verification/   │  post-action: read real state back,
                    │  (outcome)       │  confirm the effect actually happened
                    └────────┬─────────┘
                             ▼
                    ┌──────────────────┐
                    │  audit/          │  append-only; written at every step
                    └──────────────────┘
```

### 6.1 Directory layout

```
Merchant_FinPilot/
├── ARCHITECTURE.md                 this document
├── PROJECT_RULES.md                binding engineering rules — read first
├── README.md
├── requirements.txt                I/O-boundary deps only (see ADR-001)
├── .env.example
│
└── backend/
    ├── domain/          Typed contracts. Pure stdlib. Zero dependencies.
    │   ├── money.py             integer minor units; no floats
    │   ├── enums.py             closed vocabularies
    │   ├── payment.py           Payment, Order, PaymentEnrichment
    │   ├── metrics.py           FinancialMetrics, RevenueRisk, DimensionSlice
    │   ├── incident.py          FinancialIncident, FinancialEvidence
    │   ├── intent.py            AgentIntent  ← the critical contract
    │   ├── policy.py            PolicyDecision, PolicyViolation
    │   ├── execution.py         ActionResult
    │   ├── verification.py      VerificationResult
    │   ├── audit.py             AuditEvent
    │   └── errors.py
    │
    ├── financial/       DETERMINISTIC financial engine. Pure stdlib.
    │   ├── windows.py           time bucketing
    │   ├── counts.py            transaction counts (decided vs undecided)
    │   ├── rates.py             success / failure rate
    │   ├── baseline.py          baseline failure rate + comparable windows
    │   ├── deviation.py         absolute / relative deviation
    │   ├── significance.py      two-proportion z-test (a measure, not a detector)
    │   ├── exposure.py          affected txns, affected GMV, revenue at risk
    │   ├── breakdown.py         deterministic dimensional slicing
    │   └── engine.py            façade: one call → FinancialMetrics
    │
    ├── data/            Synthetic dataset + ground truth. Pure stdlib.
    │   ├── scenarios.py         11 scenario specifications
    │   ├── ground_truth.py      labels — evaluation only, never agent input
    │   └── generator.py         deterministic, seeded generator
    │
    ├── detection/       [Day 3]  deterministic incident opening
    ├── agent/           [Day 5]  LLM reasoning loop
    ├── tools/           [Day 5]  narrow read-only tool surface
    ├── verification/    [Day 6]  Financial Verifier + outcome verification
    ├── policy/          [Day 6]  Policy Engine
    ├── execution/       [Day 7]  dumb executor
    ├── razorpay/        [Day 4]  integration boundary
    ├── audit/           [Day 3]  append-only audit log
    ├── db/              [Day 3]  persistence
    ├── api/             [Day 8]  HTTP surface (thin)
    └── tests/           mirrors the tree above
```

Packages marked `[Day N]` exist as documented empty packages. They contain a
module docstring stating their contract and **no implementation**. There is no
placeholder code that looks implemented but is not.

---

## 7. Deterministic financial engine

The heart of the system. Pure functions, no I/O, no LLM, no framework.

### 7.1 Money representation

**Decision: money is an `int` count of minor units (paise). Never a float.**

Razorpay's payment entity expresses `amount` as an integer in the smallest
currency unit. We keep that representation end to end — no conversion, no
precision class of bugs. `1234` means ₹12.34.

| Quantity | Type | Rule |
|---|---|---|
| Money | `int` paise, wrapped in `Money` | Exact. Arbitrary precision. Never `float`. |
| Rate / ratio | `decimal.Decimal` | Computed at 28 significant digits; quantized only for display. |
| Count | `int` | Exact. |
| Test statistic (z, p) | `float` | **Only** permissible use of `float`. Never money, never a rate. |

`Money` rejects `float` construction at the type boundary. Currency is carried
explicitly and mixed-currency arithmetic raises. The MVP enforces INR-only.

### 7.2 Decided vs undecided population

A subtle correctness point that the engine encodes explicitly.

Razorpay payment status values include `created`, `authorized`, `captured`,
`refunded`, `failed`. A payment in `created` has **not yet succeeded or
failed** — it is in flight.

If in-flight payments are counted in the denominator, failure rate is
systematically understated and a real incident is masked. So:

```
succeeded  = {authorized, captured, refunded}     # reached money-in
failed     = {failed}
undecided  = {created}                            # excluded from rates
decided    = succeeded + failed                   # the rate denominator
```

`failure_rate = failed / decided`, and when `decided == 0` the rate is
**`None`, not `0`.** Undefined is not zero. This is what drives the
`INSUFFICIENT_DATA` path rather than a false "0% failure, all healthy" reading.

`refunded` counts as a *payment success* — the payment authorized and captured;
a refund is a separate downstream event and a separate incident class.

### 7.3 Baseline

`baseline_failure_rate(windows, method, min_decided)`

Two estimators, both deterministic:

| Method | Definition | Use |
|---|---|---|
| `POOLED` | `Σfailed / Σdecided` across baseline windows | Default. Volume-weighted, stable. |
| `MEDIAN_OF_WINDOWS` | median of per-window rates | Robust when one window is pathological. |

Two guards:

- **Comparable-window selection.** Comparing 8pm traffic against a flat 24-hour
  pooled average manufactures false positives, because evening failure rates
  differ structurally from 4am rates. `select_comparable_windows` supports
  `ALL` and `SAME_HOUR_OF_DAY`. The `EVENING_FAILURE_SPIKE` and `FALSE_ALARM`
  scenarios exist precisely to test this.
- **Minimum sample.** If pooled `decided` is below `min_decided`, the baseline
  is `None` and no deviation claim may be made.

The incident window is always excluded from its own baseline.

### 7.4 Deviation and significance

- `absolute_deviation` — percentage-point delta (`Decimal`).
- `relative_deviation` — lift ratio, `current / baseline`; `None` when baseline
  is zero (an undefined ratio, not "infinite").
- `two_proportion_z_score` / `p_value` — a **measurement of whether the
  observed difference is distinguishable from sampling noise**, via `math.erf`.

Significance is deliberately a *measure*, not a detector. It answers "could
this be noise?" Threshold-based detection (does this open an incident?) lives in
`detection/` on Day 3, so the thresholds are configurable and auditable rather
than buried in the arithmetic.

Without this, `SMALL_RANDOM_VARIATION` — thin by design, 45 decided transactions
in its incident window — is indistinguishable from a real incident, and the agent
would confidently investigate noise. (`FALSE_ALARM` is a different failure mode
entirely: it is *statistically* significant against a pooled baseline and is only
dismissed by comparable-window selection, §7.3.)

**A p-value is not sufficient on its own, and the code says so.** The
two-proportion z-test relies on a normal approximation that needs a handful of
expected events in each cell. 3 failures out of 12 against a 5% baseline scores
`z = 3.09, p = 0.002` — as confident as a genuine outage — on 0.63 expected
failures. The arithmetic is right and the conclusion is unsupportable. Large `n`
does not rescue it either: the *rare cell* is what matters, so 2 failures in
5,000 against 1 in 5,000 is equally inadmissible.

`SignificanceResult` therefore carries `min_expected_count` — the smallest of the
four expected cell counts under the pooled proportion — and the derived property
`normal_approximation_valid`, gated on
`MIN_EXPECTED_COUNT_FOR_NORMAL_APPROXIMATION = 5.0` (the inclusive textbook
threshold). It defaults to a conservative `0.0`, so a result constructed without
the field reads as inadmissible rather than admissible-by-omission. Detection
must gate on this flag as well as on `p_value`; a confident p-value on thin data
is a trap, not a signal.

### 7.5 Exposure — the money at risk

Three distinct quantities, deliberately not conflated:

| Quantity | Definition | Nature |
|---|---|---|
| `failed_gmv` | Σ amount of failed payments in the window | **Observed fact** |
| `excess_failed_transactions` | `failed − round(baseline_rate × decided)`, clamped at ≥ 0 | **Derived fact** |
| `revenue_at_risk` | `failed_gmv × (excess_failures / failed)` | **Derived fact** |
| `recoverable_revenue` | `revenue_at_risk × recovery_rate` | **ESTIMATE — requires an explicit assumption** |

Three rules the code enforces:

1. **Clamping.** If current failures are *below* baseline, excess is `0`, not
   negative. Negative revenue at risk is meaningless.
2. **Recoverable revenue is not a fact.** It depends on an unproven recovery
   rate. `recoverable_revenue()` therefore *requires* a caller-supplied
   `RecoveryAssumption` carrying a `rate`, a `source` string and a `rationale`.
   The result is flagged `is_estimate=True` and every downstream consumer must
   render it as an estimate. There is no default recovery rate, because a
   default would silently become a fact.
3. **The ratio form, not `mean_failed_ticket × excess`.** `revenue_at_risk`
   scales the *observed* failed GMV by the excess share, so the conversion from
   `Decimal` ratio to integer paise happens exactly once. Multiplying a
   pre-rounded mean ticket rounds twice and biases the result systematically:
   failures of 100, 100 and 101 paise with 2 of 3 excess give `301 × 2/3 = 201`
   the correct way and `(301/3 → 100) × 2 = 200` the naive way. One paisa on
   three transactions, and a growing understatement at scale. `mean_failed_ticket`
   remains on `RevenueRisk` as a reported figure; it is not an input to
   `revenue_at_risk`.

Rounding is always explicit: `ROUND_HALF_UP` to whole paise at the single
final step. No intermediate rounding.

`failed_gmv` and `revenue_at_risk` answer different questions and the gap between
them is large. A healthy business fails payments every hour, so quoting observed
failed GMV as the cost of an incident overstates it by exactly `failed / excess`:
1,000 decided with 100 failures at ₹100 is ₹10,000 of failed GMV but ₹5,000 at
risk against a 5% baseline and ₹2,000 against an 8% one — a 5× overstatement in
the second case. `revenue_at_risk ≤ failed_gmv` always, with equality only when
the baseline is zero.

---

## 8. Agent workflow

The agent is a bounded loop over read-only tools, ending in a proposal.

```
 1. TRIGGER      deterministic detection opens a FinancialIncident.
                 The agent never decides that an incident exists.

 2. ORIENT       agent reads the incident: window, metric, deviation,
                 significance — all precomputed by financial/.

 3. INVESTIGATE  bounded loop (hard cap on iterations and tool calls):
                   • agent picks the next dimension to slice
                   • tool returns a slice computed by financial/
                   • each result is appended to FinancialEvidence with an
                     evidence_id
                 The agent chooses WHAT to look at. It never computes.

 4. REASON       agent forms a root-cause hypothesis citing evidence_ids.
                 A claim with no evidence reference is rejected at validation.

 5. PROPOSE      agent emits exactly one AgentIntent:
                 action · target · parameters · reason · evidence_refs
                 This is a proposal. Nothing has happened yet.

 6. VERIFY       Financial Verifier (deterministic) independently re-derives
                 every number in the intent from source records, and validates
                 live payment/order state. → VerifiedIntent | rejection.

 7. AUTHORIZE    Policy Engine (deterministic) → ALLOW | BLOCK | ESCALATE.

 8. EXECUTE      only on ALLOW. Dumb executor, idempotency key, one bounded
                 Razorpay action.

 9. CONFIRM      post-action verification reads real Razorpay state back and
                 confirms the intended effect. The agent's opinion that it
                 worked is worthless here.

10. EXPLAIN      agent narrates what happened for the merchant. Narration only —
                 it cannot change any recorded fact.
```

Steps 1, 6, 7, 8, 9 contain **no LLM call whatsoever**. Steps 2–5 and 10 are the
LLM's entire remit.

### 8.1 Separation of reasoning from execution

The reasoning loop's only output is an `AgentIntent` object. It holds no client,
no session, no credential, and no reference to the executor. It is a pure
function from evidence to proposal, which is why it can be replayed against a
recorded incident in tests without any possibility of side effects.

---

## 9. Tool architecture

Tools are the LLM's only window onto the world, so the tool surface *is* the
security boundary.

**Invariants:**

1. **Read-only during investigation.** No tool available to the reasoning loop
   mutates state or calls a Razorpay write endpoint. The only "write" the agent
   can perform is emitting an intent, which is a proposal.
2. **Narrow, single-purpose.** `get_failure_breakdown_by_method(incident_id)`,
   not `query(sql)`. No generic query tool, no code execution, no HTTP tool.
3. **Explicit schemas.** Every parameter typed, bounded and enumerated where
   possible. Free-text parameters are rejected unless there is a reason.
   IDs are validated against the incident's own scope — the agent cannot pivot
   to another merchant's or another incident's data.
4. **Computation happens in `financial/`.** A tool is a thin adapter: validate
   args → call a deterministic function → return a typed result. A tool never
   contains financial arithmetic of its own.
5. **Every call is audited** with arguments and result digest.
6. **Bounded.** Hard caps on tool calls per investigation, rows per result and
   time window breadth. An unbounded loop is a cost and a correctness risk.

Planned Day-5 tool surface (read-only):

```
get_incident_summary(incident_id)
get_failure_breakdown(incident_id, dimension)     # method|reason|region|provider|hour
get_time_series(incident_id, bucket)              # hourly|daily
get_baseline_comparison(incident_id, dimension_value)
get_sample_failed_payments(incident_id, limit)    # limit hard-capped
get_revenue_exposure(incident_id)
check_action_eligibility(incident_id, action)     # deterministic pre-check
```

Note `check_action_eligibility`: the agent may *ask* whether an action is
eligible, and the answer is computed deterministically. This lets the agent
avoid proposing something that will obviously be blocked, without giving it any
authority over the answer.

---

## 10. Financial Verifier

An independent deterministic re-derivation of the agent's claims. Its design
premise is that **the agent may be wrong, stale, or adversarial.**

Input: `AgentIntent` + incident. Output: `VerificationResult`.

| Check | Failure mode it defends against |
|---|---|
| Recompute every numeric claim from source records | Hallucinated / miscalculated amounts |
| Evidence references resolve, and support the claim | Fabricated citations |
| Evidence is fresh (within a max staleness window) | Acting on facts that have since changed |
| Amounts within tolerance of re-derived values | Silent drift, off-by-100 (rupee/paise) errors |
| Target entity exists in Razorpay | Invented payment/order IDs |
| Target entity state permits the action | Refunding an uncaptured payment, etc. |
| Currency matches and is INR | Currency confusion |
| Intent schema and enum membership | Out-of-vocabulary actions |
| Intent scope ⊆ incident scope | Privilege escalation via scope widening |

**Tolerance policy:** amount comparison is exact for money that must match a
known record (`==` on integer paise). A tolerance is permitted only for
derived aggregate estimates, is expressed as an explicit basis-point bound, and
is recorded in the `VerificationResult`. Verification is not a rounding fixer:
it does not correct the agent's number, it **rejects** it.

The verifier is why the LLM can be treated as untrusted input.

---

## 11. Policy Engine

Deterministic authorization. Runs **after** verification, on a `VerifiedIntent`.
Never sees or asks the LLM anything.

Output: `PolicyDecision { verdict, violations[], required_approvals[], rationale }`
where `verdict ∈ {ALLOW, BLOCK, ESCALATE}`.

Rule families:

| Family | Examples |
|---|---|
| Kill switch | `FINPILOT_EXECUTION_ENABLED=false` → BLOCK everything |
| Mode guard | `RAZORPAY_MODE != test` → BLOCK (MVP) |
| Action allowlist | Only explicitly enumerated actions are executable |
| Amount limits | Per-action cap, per-incident cap, daily aggregate cap |
| Rate limits | Max actions per incident / per hour / per merchant per day |
| Duplicate prevention | No second action for the same (incident, action, target) |
| Cooldown | Minimum interval between actions on the same target |
| Eligibility | Deterministic preconditions for the specific action |
| Evidence sufficiency | Minimum evidence count + significance before acting |
| Confidence floor | Below a floor → ESCALATE, never ALLOW |
| Blast radius | Above an affected-entity count → ESCALATE |

**Design rules:**

- **Fail closed.** Any error, missing input, unknown action, unparseable
  intent, or unhandled case ⇒ not-ALLOW. The default is never permission.
- **Ambiguity escalates.** ESCALATE is a first-class, frequently-correct
  outcome, not a failure. A system that only ever ALLOWs or BLOCKs is
  overconfident.
- **Every rule is an independent, individually testable pure function** of
  `(verified_intent, context) → PolicyViolation | None`.
- **All violations are collected**, not short-circuited on the first, so the
  audit trail explains every reason.
- The engine is **data-in / decision-out**: no I/O, no clock reads (time is
  injected), fully reproducible from an audit record.

---

## 12. Razorpay integration boundary

All Razorpay contact is confined to `backend/razorpay/`. No other package
imports the SDK or knows a URL. Everything else speaks domain contracts.

### 12.1 Verification status of Razorpay capabilities

Confidence is stated honestly. Nothing marked below as requiring verification
may be coded against until checked against official documentation and this
table updated.

**Reasonably confident (still to be confirmed against official docs before use):**

| Capability | Note |
|---|---|
| Payment entity: `id`, `amount` (integer paise), `currency`, `status`, `method`, `order_id`, `created_at` (unix), `error_code`, `error_description`, `error_source`, `error_step`, `error_reason` | Field-level shape to be re-confirmed at integration time |
| Payment `status` vocabulary: `created`, `authorized`, `captured`, `refunded`, `failed` | |
| Payment `method` vocabulary: `card`, `netbanking`, `wallet`, `upi`, `emi` | |
| Order entity + `status`: `created`, `attempted`, `paid` | |
| Fetch a payment by id; fetch/list payments over a time range | Pagination params and max page size: **REQUIRES OFFICIAL DOC VERIFICATION** |
| Webhooks exist for payment lifecycle events including a payment failure event | Exact event names: **REQUIRES OFFICIAL DOC VERIFICATION** |
| Webhook authenticity via HMAC-SHA256 over the **raw** request body against a signature header, using the endpoint's webhook secret | Exact header name and canonical payload: **REQUIRES OFFICIAL DOC VERIFICATION** |
| Payment Links API exists | Exact request/response schema: **REQUIRES OFFICIAL DOC VERIFICATION** |

**`TBD` / `REQUIRES OFFICIAL DOC VERIFICATION` — assume unavailable until proven:**

| Item | Status |
|---|---|
| Idempotency-key header support, and which endpoints honour it | **REQUIRES OFFICIAL DOC VERIFICATION.** Until confirmed, idempotency is enforced **on our side** (§15). |
| A `region` / `geography` field on the payment entity | **TBD — assume it does not exist.** Treated as internally derived enrichment (§12.2). |
| A stable `provider` / `acquirer` / `route` dimension. `acquirer_data` exists but its contents vary by method | **REQUIRES OFFICIAL DOC VERIFICATION.** Until confirmed, provider is internal enrichment. |
| Any API to change payment-method availability, routing or acquirer selection at runtime | **TBD — assume unavailable.** No proposed action may depend on it. |
| Aggregate analytics / metrics endpoints | **TBD — assume unavailable.** We aggregate ourselves. |
| Programmatic retry of a failed payment | **TBD.** Do not design a recovery action around this until verified. |
| Rate limits and quotas | **REQUIRES OFFICIAL DOC VERIFICATION.** |
| Test-mode support for each write action we might take | **REQUIRES OFFICIAL DOC VERIFICATION per action.** |

**Standing rule:** an endpoint, parameter, field or event name that has not been
read in official Razorpay documentation does not exist. See PROJECT_RULES §6.

### 12.2 Derived enrichment

`region`/`segment` and `provider`/`route` are investigation dimensions the
product needs but which we cannot yet source from the payment entity. They are
therefore modelled as a **separate, explicitly-labelled `PaymentEnrichment`
record** joined to the payment — never as fields on `Payment` itself.

This keeps the distinction visible everywhere: `Payment` is what Razorpay told
us; `PaymentEnrichment` is what we inferred. Evidence built on enrichment is
tagged with a lower `source_confidence`, and no policy rule may be gated solely
on an enrichment-derived fact without an explicit note.

In the synthetic dataset these dimensions are generated with known ground truth,
so the investigation logic can be built and evaluated now and re-pointed at a
verified source later without touching the engine.

### 12.3 Adapter responsibilities

Inbound: verify webhook signature → reject unverified → parse → map to domain
contracts → persist raw payload for audit → hand off. Unknown fields are
preserved in the raw record, never silently dropped.

Outbound: accept a fully-authorized action, attach an idempotency key, call
exactly one documented endpoint, return the raw response for verification.
The adapter contains **no policy logic and no financial arithmetic.**

---

## 13. Execution layer

**The executor is deliberately stupid.** Intelligence in the executor is a
security bug: it becomes a second, untested authorization path.

The executor:

- Accepts only an authorized action carrying a valid `PolicyDecision(ALLOW)`.
- Re-checks the decision's integrity and freshness before acting (a decision
  is not a bearer token valid forever — it has a short TTL).
- Attaches an idempotency key derived deterministically from
  `(incident_id, action, target, canonical_parameters)`.
- Performs exactly one bounded outbound call.
- Records `ActionResult` — including on failure, including on timeout.
- Never retries a consequential action on an ambiguous outcome; it records
  `UNKNOWN` and escalates. A timeout is not a failure: the action may have
  succeeded.
- Never decides, never computes, never interprets, never falls back.

What it must not do: no policy checks, no amount computation, no
"the amount looks wrong so I'll fix it", no automatic retry loops, no
"if the action failed, try a different action".

---

## 14. Verification layer (post-action)

Two verifications, both deterministic, both mandatory.

**Pre-execution (§10)** — is the intent correct and safe?

**Post-execution — did the intended thing actually happen, in reality?**

```
1. Was the API response well-formed and successful?          (response check)
2. Read the real entity state back from Razorpay.            (state check)
3. Does observed state match the intended effect?            (effect check)
4. Do the financial numbers still reconcile?                 (reconciliation)
5. Any unintended side effect (duplicate created, wrong amount)? (blast check)
```

Rules:

- A `2xx` response is **not** proof of a financial effect. Only reading state
  back is.
- The agent's assertion that an action succeeded carries **zero** weight and is
  not an input to this layer.
- Mismatch ⇒ `VerificationResult(status=MISMATCH)` + escalate + audit. Never
  auto-remediate a mismatch: a wrong compensating action on top of an unclear
  state is how one bad action becomes two.
- Unverifiable outcome ⇒ `UNKNOWN`, escalate. Do not guess.

Deferred effects (an outcome that only resolves later, e.g. whether a customer
paid a link) are modelled as a **scheduled re-verification**, not a synchronous
check that lies.

---

## 15. Idempotency principles

Money duplicates are the worst class of bug here, so idempotency is designed at
four layers rather than assumed at one.

| Layer | Mechanism |
|---|---|
| Webhook ingestion | Dedupe on Razorpay event id (plus payload digest). Redelivery is expected, not exceptional. Handlers are idempotent. |
| Incident creation | A stable incident key from (merchant, metric, dimension, window) prevents re-opening the same incident every poll. |
| Intent | A content hash over the canonical intent. The same proposal is recognised, not re-executed. |
| Execution | An `execution_key` derived from `(incident_id, action, target, canonical_parameters)`, persisted with a unique constraint **before** the outbound call. A pre-existing key short-circuits and returns the recorded result. |

Additional rules: the unique constraint is claimed *before* the call, so a crash
mid-call cannot produce a second attempt. Ambiguous outcomes are never retried
automatically. Canonical serialization (sorted keys, no floats, fixed encoding)
is required for any hash — an unstable hash is not an idempotency key.

Razorpay-side idempotency support is **`REQUIRES OFFICIAL DOC VERIFICATION`**
(§12.1). We therefore do not depend on it: our own layer must be sufficient on
its own, and provider support, once verified, becomes defence in depth.

---

## 16. Audit trail

Append-only, immutable, complete. If it isn't audited, it didn't happen.

Every `AuditEvent` records: monotonic sequence, timestamp, incident id, actor
(`SYSTEM` | `AGENT` | `POLICY` | `EXECUTOR` | `HUMAN`), event type, subject,
payload digest, and payload.

Audited at minimum: every fact ingested, every metric computed (with inputs),
every tool call and result, the agent's full reasoning trace and model/prompt
version, the intent, the verification result with every check, the policy
decision with every violation and the rule versions, the execution attempt and
raw response, the outcome verification, and every escalation and human action.

Properties:

- **Append-only.** No update, no delete. A correction is a new event.
- **Replayable.** A reviewer can reconstruct exactly why a decision was made —
  including a decision that was wrong.
- **Never contains secrets.** Keys, tokens and signatures are redacted at write
  time, not at read time.
- **Both inputs and outputs** of every deterministic computation, so any number
  can be independently recomputed later.

The audit trail is the accountability authority. It is what makes an autonomous
financial agent defensible.

---

## 17. Failure handling

Default posture: **degrade to safe, never to permissive.**

| Failure | Behaviour |
|---|---|
| LLM unavailable / slow / over budget | Incident stays open, undiagnosed. No action. Deterministic detection and metrics still work — the system is useful without the LLM. |
| LLM returns malformed intent | Reject on schema. One bounded reprompt. Then escalate. Never coerce a malformed intent into a valid one. |
| LLM proposes a disallowed action | BLOCK + audit. Treated as expected adversarial input, not a crash. |
| Verifier finds a number mismatch | BLOCK + audit. Never silently correct. |
| Insufficient data | No incident, no claim. `INSUFFICIENT_DATA` is a correct answer. |
| Razorpay read fails | Retry with backoff (reads are safe to retry). Metrics marked stale. No action on stale data. |
| Razorpay write fails cleanly | Record failure, do not retry automatically, escalate. |
| Razorpay write times out | Outcome `UNKNOWN`. **Never retry.** Verify actual state, then escalate. |
| Webhook signature invalid | Drop, audit, alert. Never process. |
| Duplicate event | Idempotent no-op, audited. |
| Policy engine error | Fail closed → BLOCK. |
| Audit write fails | **Abort the operation.** An unauditable consequential action must not proceed. |
| Clock skew / out-of-order events | Event time from the source of truth; ingestion time recorded separately. |

Two invariants worth stating on their own:

1. **A crash must never leave money moved and unrecorded.** The execution key
   and the intent to act are persisted before the outbound call.
2. **No failure path leads to a broader permission than the success path.**

---

## 18. Security principles

- **Least privilege for the LLM.** Narrow read-only tools; no SQL, no shell, no
  HTTP, no filesystem, no write endpoints. The tool surface is the boundary.
- **The LLM is untrusted input.** Its output is validated like a form submitted
  by a stranger. Prompt injection is assumed possible, so injection cannot
  reach anything consequential: any injected instruction still has to pass the
  verifier and the policy engine, which never consult the model.
- **Secrets only from the environment.** Never hardcoded, never logged, never
  committed, never placed in an audit payload or an LLM prompt.
- **Test mode only** for the MVP, enforced by a policy guard, not by convention.
- **Webhook authenticity mandatory.** Unsigned or mis-signed payloads are not
  data.
- **Validate every external input** at the boundary — Razorpay payloads, HTTP
  requests, LLM output, dataset files.
- **Never trust frontend financial values.** The frontend displays money; it is
  never a source of it. Any amount arriving from a client is discarded and
  re-derived server-side.
- **Defence in depth.** Verifier, policy engine, idempotency layer and outcome
  verification are independent. No single failure authorizes an unsafe action.
- **Global kill switch** that halts all execution regardless of any decision.

---

## 19. Testing and evaluation strategy

Two distinct activities, deliberately separated.

### 19.1 Deterministic tests (correctness)

Standard library `unittest`, no third-party runner needed, no network, no LLM,
no clock dependence (time is always injected).

| Layer | What is tested |
|---|---|
| `domain/` | Invariants: no float money, currency mismatch raises, enum closure, immutability |
| `financial/` | **Every function, individually.** Known-value cases, boundaries (0, 1, all-fail, all-success, undefined), rounding, clamping, the `success + failure == 1` identity |
| `data/` | Byte-identical output for a fixed seed; ground-truth labels match generated reality; no label leakage into agent inputs |
| `detection/` | Fires on real incidents, silent on `FALSE_ALARM` and `SMALL_RANDOM_VARIATION` |
| `policy/` | Each rule in isolation; fail-closed on malformed input; **adversarial intents** |
| `verification/` | Detects inflated amounts, fabricated evidence refs, invented IDs, stale evidence, scope widening |
| `execution/` | Duplicate suppression, key stability, no retry on ambiguity |
| `api/` | Contract shape only; no business logic to test there by construction |

Mandatory adversarial cases (the agent is assumed hostile):

- Intent whose amount is 100× the verified figure (rupee/paise confusion).
- Intent citing an `evidence_id` that does not exist.
- Intent targeting a payment id outside the incident.
- Intent for an action not on the allowlist.
- Intent with a negative, zero, or absurdly large amount.
- Two identical intents submitted concurrently.
- Intent produced from evidence that has since gone stale.
- Well-argued, confident, entirely wrong reasoning.

### 19.2 Scenario evaluation (agent quality)

The 11 ground-truth scenarios form the evaluation set. Ground truth is
**structurally separated** from agent input: labels live on
`SyntheticPayment`/`GroundTruth` in `data/`, and the production path receives
plain `Payment` objects that have no label fields at all. A test asserts this.

Scored per scenario:

| Dimension | Question |
|---|---|
| Detection | Incident opened when it should be, and only then? |
| Localisation | Correct primary dimension identified? |
| Root cause | Hypothesis matches ground-truth cause? |
| Quantification | Do reported figures equal the deterministic values exactly? |
| Action | Proposed action appropriate and eligible? |
| Authorization | Correct verdict for the case? |
| Restraint | **Does the agent do nothing when nothing should be done?** |

Restraint is weighted as heavily as detection. `FALSE_ALARM`,
`SMALL_RANDOM_VARIATION`, `INSUFFICIENT_DATA` and `RECOVERY_NOT_ELIGIBLE`
exist to fail an over-eager agent. An agent that acts on everything is worse
than no agent.

---

## 20. Future extensibility

The architecture generalises along one axis: **incident class**. The loop
(detect → investigate → quantify → propose → verify → authorize → execute →
verify outcome → audit) is class-independent.

Adding an incident class requires: financial metrics for it, a detector,
dimensions to slice, an action allowlist, policy rules, and eligibility +
outcome verification. It requires **no change** to the agent loop, the verifier
contract, the policy engine core, the executor, or the audit trail.

Candidates: settlement delay/mismatch, refund anomalies, dispute/chargeback
spikes, subscription churn and involuntary churn, payout failures, fee/pricing
drift, reconciliation breaks, cash-flow forecasting.

Deliberately deferred: multi-currency, multi-tenant, streaming ingestion,
learned baselines, autonomy tiers earned from a verified track record,
multi-agent decomposition, cross-merchant benchmarking.

---

## 21. Architecture decision records

### ADR-001 — The financial core has zero third-party dependencies

`backend/domain/`, `backend/financial/` and `backend/data/` import only the
Python standard library.

*Rationale.* The layer that defines financial truth should not inherit the
version semantics or coercion behaviour of a validation library or a web
framework. It stays testable in seconds, anywhere, with no install step. A
practical trigger reinforced this: the Day-2 build environment had no package
index access, and the entire core plus its test suite was built and run anyway —
a useful proof that the truth layer is genuinely self-contained.

Pydantic and FastAPI enter **only** at the I/O boundary (`api/`, LLM tool-arg
parsing, Razorpay payload parsing), where coercion of untrusted external input
is the actual job.

*Consequence.* We use `dataclasses`, `enum`, `decimal` and `unittest`. Contracts
validate themselves in `__post_init__` rather than via a schema library. Tests
are `unittest.TestCase` subclasses, which `pytest` can also run unchanged.

### ADR-002 — Modular monolith

One deployable with enforced internal seams (`domain` ← `financial` ← everything
else; no reverse imports). A 10-day build cannot afford distributed-systems
failure modes, and every seam here is a module boundary, not a network boundary.
Splitting later is mechanical because the seams already exist.

### ADR-003 — Integer paise, `Decimal` rates, `float` only for statistics

See §7.1. Matches Razorpay's own integer-minor-unit representation, eliminates
float money bugs by construction.

### ADR-004 — Undefined is not zero

Rates over an empty population return `None`. Baselines with insufficient
samples return `None`. Ratios over a zero denominator return `None`.
This forces callers to handle "we don't know", which is what makes
`INSUFFICIENT_DATA` a first-class outcome instead of a silent "0% — healthy".

### ADR-005 — Ground truth is structurally separated, not merely conventionally

`Payment` (production contract) has no label fields. `SyntheticPayment`
(dataset only) wraps a `Payment` and adds `scenario_id` and labels, exposing
`.to_payment()`. Leakage is impossible by construction rather than by
discipline, and a test enforces it.

### ADR-006 — Significance is a measurement, not a detector

`financial/significance.py` computes a z-score and p-value; it does not decide
anything. Thresholds live in `detection/` (Day 3) so they are configurable,
auditable and testable independently of the arithmetic.

*Extension (Day 2).* Because a p-value alone is not admissible evidence,
`SignificanceResult` also reports `min_expected_count` and
`normal_approximation_valid` (§7.4). A thin sample can produce `p = 0.002` while
the normal approximation it was computed under does not hold; reporting the
p-value without its own validity condition would hand `detection/` a number that
looks like confidence and isn't. The measurement layer therefore states the limits
of its own method, and the detector is required to read them.

### ADR-007 — Recoverable revenue requires an explicit assumption

There is no default recovery rate. `recoverable_revenue()` demands a
`RecoveryAssumption` with a source and rationale, and returns a value flagged
`is_estimate=True`. A defaulted assumption silently becomes a fact.

---

## 22. Open architectural questions

Recorded rather than silently decided. Each blocks a specific later decision.

| # | Question | Blocks | Current working assumption |
|---|---|---|---|
| Q1 | Will we have real Razorpay test-mode API credentials, and which write actions are available in test mode? | Choice of executable action (Day 6–7) | Assume test mode with, at most, Payment Links. Design so the action is pluggable. |
| Q2 | What is the **one** consequential action for the MVP demo? It must be officially documented, test-mode-safe, and verifiable after the fact. | Execution + outcome verification | Not decided. §12.1 rules out routing/method-availability changes for now. |
| Q3 | Does ESCALATE mean a human approves in the live demo, or is it a terminal state we merely display? | Demo flow, API surface, UI | Assume a real approval step — it is a stronger demo and exercises the path. |
| Q4 | Real Razorpay data, synthetic data, or both, in the demo? | Ingestion + credibility | Assume both: synthetic for reproducible scenarios, real test-mode data to prove the integration. |
| Q5 | Is INR-only acceptable for judging? | `Money` guard strictness | Assume yes; multi-currency is a documented non-goal. |
| Q6 | Baseline default: pooled 7-day, or same-hour-of-day? | Detection quality, false-alarm rate | Both implemented. Default to be chosen on Day 3 from measured false-alarm rates on the scenario set. |
| Q7 | Is a human-authored recovery-rate assumption acceptable for `recoverable_revenue`, or should the MVP report only `revenue_at_risk`? | Whether estimates appear in the product at all | Assume `revenue_at_risk` (a fact) is primary and always shown; recoverable revenue only where an assumption is explicitly attached. |
| Q8 | `backend/razorpay/` shares its name with Razorpay's PyPI distribution. Keep the name, or rename to `razorpay_gateway`? | Day-4 SDK adoption | Keep the prescribed name. Absolute imports mean `import razorpay` inside the package resolves to the SDK, which is correct as long as `backend/` is never on `sys.path` — it isn't, since the suite runs with `PYTHONPATH=.` from the repo root. Revisit if the SDK is adopted and the collision bites. |
