# PROJECT_RULES.md — Binding Engineering Rules

**Merchant FinPilot** · Razorpay Buildathon 2026 · Track 5

> **READ THIS FILE AND [ARCHITECTURE.md](ARCHITECTURE.md) BEFORE MODIFYING ANY CODE.**
> This applies to every human and to every AI coding agent (Claude, Codex, or
> otherwise) that touches this repository.

These are not style suggestions. This system reasons about and acts on money.
A rule violation here is a financial-safety defect, not a code-review nit.

**Rule 0 — If a change would violate a rule in this document, do not make the
change. Stop and say so.** Do not work around a rule, do not weaken it to make
a test pass, and do not delete it to unblock yourself. If a rule is genuinely
wrong, say why and propose amending this file — as a separate, explicit change.

**Precedence:** PROJECT_RULES.md > ARCHITECTURE.md > existing code > convenience.

---

## 1. Financial safety (non-negotiable)

1.1 **The LLM must never directly mutate financial state.** No LLM output is
ever passed to a write API, a database write, or a state transition without
passing through the Financial Verifier and the Policy Engine first.

1.2 **Never trust an LLM-generated financial calculation.** Any number an LLM
produces is display text until Python re-derives it from source data. If Python
cannot re-derive it, the number does not exist and must not be shown as a fact.

1.3 **Never let an LLM-generated amount bypass deterministic validation.** Every
amount reaching an action is re-derived from source records and compared. A
mismatch is a `BLOCK`, never a silent correction and never a warning.

1.4 **Never execute a financial action without an explicit `PolicyDecision`
with verdict `ALLOW`.** Absence of a decision is not permission. An expired
decision is not permission. A decision for a different intent is not permission.

1.5 **Never assume an API call succeeded.** A `2xx` is not proof of a financial
effect. Read the real state back and verify. A timeout means `UNKNOWN`, not
failure — and `UNKNOWN` is never retried automatically.

1.6 **Money is an integer count of minor units (paise).** Never `float`, never
a formatted string, never a rupee-denominated number in internal code. Rates
are `decimal.Decimal`. `float` is permitted **only** for statistical test
statistics (z-score, p-value) and never for money, a rate, or anything that
feeds an amount.

1.7 **Undefined is not zero.** A rate over an empty population is `None`. A
baseline with insufficient samples is `None`. A ratio with a zero denominator is
`None`. Never substitute `0` for "unknown", and never let `None` silently
become `0` downstream.

1.8 **Clamp impossible values, do not emit them.** Excess failures below zero is
`0`. Negative revenue at risk does not exist. Rates outside `[0, 1]` are a bug —
raise, do not clip silently.

1.9 **Estimates must be labelled as estimates.** Any figure resting on an
assumption (e.g. a recovery rate) carries the assumption, its source, and
`is_estimate=True`, and must be rendered as an estimate everywhere. Never
default an assumption — a defaulted assumption silently becomes a fact.

1.10 **A financial number without a traceable derivation must not be displayed.**
If you cannot point to the function and inputs that produced it, remove it.

---

## 2. Data and source of truth

2.1 **The backend database and Razorpay are the only sources of financial
truth.** In a conflict, Razorpay wins for external payment/order state.

2.2 **Never trust a financial value from the frontend.** Amounts, counts and
rates arriving from a client are discarded and re-derived server-side. The
frontend displays money; it never supplies it.

2.3 **Preserve source-of-truth identifiers verbatim.** Razorpay ids
(`pay_…`, `order_…`, event ids) are stored exactly as received — never
regenerated, reformatted, truncated, lower-cased or inferred.

2.4 **Validate every external input at the boundary** — Razorpay payloads, HTTP
requests, LLM output, webhook bodies, dataset files. Reject invalid input; do
not coerce it into something plausible.

2.5 **Never drop unknown fields silently.** Persist the raw payload for audit
even when only part of it is mapped.

2.6 **Distinguish observed from derived.** What Razorpay told us
(`Payment`) is separate from what we inferred (`PaymentEnrichment`). Never merge
inferred dimensions into the observed record.

2.7 **Ground truth is evaluation-only.** Scenario labels must never reach a
production code path or an LLM prompt. Keep the structural separation
(`SyntheticPayment` → `.to_payment()`) intact; the test that enforces it must
never be weakened.

2.8 **Event time and ingestion time are different fields.** Never conflate them.
Never derive a financial window from ingestion time.

---

## 3. Agent rules

3.1 **The agent outputs a structured `AgentIntent`. It never executes.** The
reasoning loop holds no API client, no credential, and no reference to the
executor.

3.2 **The agent cannot call unrestricted APIs.** No SQL tool, no HTTP tool, no
shell tool, no code-execution tool, no filesystem tool, no generic "query"
tool. Ever. Adding one is a security defect, not a feature.

3.3 **Tools expose narrowly scoped, single-purpose capabilities.** One question
per tool. Parameters typed, bounded, enumerated where possible. Ids validated
against the current incident's scope so the agent cannot pivot to data outside
it.

3.4 **Tool schemas must be explicit and complete** — every parameter typed and
documented, every result a typed contract. No `**kwargs`, no free-form dicts, no
untyped passthrough.

3.5 **A tool never computes financial values itself.** It validates arguments,
calls a deterministic function in `backend/financial/`, and returns a typed
result. Duplicating arithmetic inside a tool is a rule violation.

3.6 **During investigation every tool is read-only.** No tool available to the
reasoning loop mutates state or touches a Razorpay write endpoint.

3.7 **Reasoning must be separable from execution.** It must be possible to
replay an entire investigation against recorded data with zero side effects. If
that is not possible, the boundary is broken — fix the boundary.

3.8 **The agent must cite evidence.** Every claim references an `evidence_id`
that resolves. An uncited claim is rejected at validation, not tidied up.

3.9 **Investigation is bounded.** Hard caps on iterations, tool calls, result
rows and window breadth. Every cap has a defined behaviour on being hit
(stop and escalate), never an unbounded loop.

3.10 **Treat all LLM output as untrusted, possibly adversarial input.** Assume
prompt injection is possible. Injection must not be able to reach anything
consequential, because the verifier and policy engine never consult the model.

3.11 **The agent never decides that an incident exists.** Detection is
deterministic and upstream.

3.12 **Record the full reasoning trace, model id and prompt version** with every
intent. An unauditable decision is not acceptable.

---

## 4. Financial computation

4.1 **All financial calculations are deterministic pure functions** — same
inputs, same outputs, always. No clock reads (inject time), no randomness, no
I/O, no network, no global mutable state, no environment reads.

4.2 **One canonical implementation per financial concept.** Failure rate is
computed in exactly one place. If you need it elsewhere, import it. Duplicated
financial logic is a defect even when both copies agree today.

4.3 **Every financial function has unit tests**, including boundary cases
(`0`, `1`, empty population, all-success, all-failure, undefined) and at least
one known-value case computed by hand.

4.4 **Never perform meaningful financial arithmetic inside a prompt**, a
template, an f-string, a route handler, a tool wrapper, or a frontend component.
Arithmetic lives in `backend/financial/` and nowhere else.

4.5 **Rounding is explicit and happens once.** State the mode
(`ROUND_HALF_UP`) and the target unit (whole paise). No intermediate rounding.
Never rely on a language default.

4.6 **Do not mix currencies.** Arithmetic across different currency codes
raises. The MVP is INR-only and enforces it.

4.7 **Keep the population definition explicit.** In-flight payments
(`created`) are excluded from rate denominators; `decided = succeeded + failed`.
Do not change this definition without updating ARCHITECTURE.md §7.2 and its
tests.

4.8 **`backend/domain/`, `backend/financial/` and `backend/data/` import only
the Python standard library** (ADR-001). Never add a third-party import to these
packages. Never make them import `api`, `agent`, `razorpay`, or `db`.

4.9 **Do not add anomaly-detection sophistication into the arithmetic layer.**
Measurement and detection are separate (ADR-006). Thresholds live in
`detection/`.

---

## 5. Policy

5.1 **Policy checks are deterministic.** No LLM call, no network call, no clock
read (inject time), no randomness. A decision must be reproducible from its
audit record.

5.2 **Every policy rule is an explicit, individually testable pure function**
of `(verified_intent, context) → PolicyViolation | None`.

5.3 **Fail closed.** Any error, missing input, unknown action, unparseable
intent or unhandled case results in not-ALLOW. `ALLOW` is only ever returned by
an explicit, successful, fully-evaluated path. There is no default-allow branch
and no `except: pass` anywhere near policy.

5.4 **Ambiguity escalates.** When rules are satisfied but confidence, evidence
sufficiency or blast radius is marginal, return `ESCALATE`. `ESCALATE` is a
correct outcome, not a failure to decide.

5.5 **Collect all violations; do not short-circuit** on the first. The audit
trail must state every reason.

5.6 **Policy runs after verification, on a verified intent.** Never authorize an
unverified intent.

5.7 **The action allowlist is explicit.** An action not enumerated in policy is
not executable, regardless of how sensible it seems.

5.8 **Never bypass policy for testing, demos, or convenience.** No
`skip_policy` flag, no `dry_run=False` shortcut, no debug branch. Tests
construct decisions explicitly; they do not disable the engine.

5.9 **Policy rule versions are recorded** with each decision so a past decision
remains explicable after the rules change.

---

## 6. Razorpay integration

6.1 **Never invent an endpoint, parameter, field, event name or status value.**
If it has not been read in official Razorpay documentation, it does not exist.

6.2 **Verify against official documentation before integrating**, then record
the finding in ARCHITECTURE.md §12.1 and update its verification status in the
same change.

6.3 **Anything unverified is marked `TBD` / `REQUIRES OFFICIAL DOC
VERIFICATION` and treated as unavailable.** Never write code that depends on an
unverified capability, not even behind a flag.

6.4 **All Razorpay contact is confined to `backend/razorpay/`.** No other
package imports the SDK, constructs a URL, or knows an endpoint name.

6.5 **Webhook authenticity is mandatory.** Verify the HMAC signature over the
**raw** request body before parsing. Use a constant-time comparison. An
unverified payload is dropped and audited — never processed, never "processed
with a warning".

6.6 **Handle idempotency on our side regardless of provider support** (§7).
Provider-side idempotency, once verified, is defence in depth — never the only
layer.

6.7 **Treat Razorpay as authoritative for external payment/order state.** Do not
infer state we could read, and do not cache state across a decision boundary
without a freshness check.

6.8 **Reads may be retried with backoff. Consequential writes may not be
retried automatically.**

6.9 **Test mode only.** The MVP must refuse to execute against live mode, and
that refusal is a policy guard, not a convention.

6.10 **Never log or audit credentials, signatures or raw secrets.** Redact at
write time.

---

## 7. Execution

7.1 **The execution layer is dumb and policy-agnostic.** It contains no policy
logic, no financial arithmetic, no eligibility checks, no interpretation, and no
fallback behaviour. Intelligence in the executor is a security defect: it
becomes a second, untested authorization path.

7.2 **Execution occurs only after verification and an `ALLOW` decision**, and
only while that decision is unexpired and matches the intent being executed.

7.3 **Prevent duplicate execution.** Derive an `execution_key` from
`(incident_id, action, target, canonical_parameters)`, persist it under a unique
constraint **before** the outbound call, and short-circuit to the recorded
result if it already exists.

7.4 **Canonical serialization is required for any key or hash** — sorted keys,
no floats, fixed encoding. An unstable hash is not an idempotency key.

7.5 **One authorized action per execution call.** No batching, no chaining, no
"while we're here" side effects.

7.6 **Record every consequential action** — attempt, parameters, raw response,
outcome — including on failure, timeout, and exception. Persist the intent to
act *before* the call so a crash cannot move money unrecorded.

7.7 **Never retry an ambiguous outcome.** Record `UNKNOWN`, verify actual state,
escalate.

7.8 **If the audit write fails, abort the operation.** An unauditable
consequential action must not proceed.

---

## 8. Verification

8.1 **Verify the API response shape and status** — but never treat `2xx` as
proof of a financial effect.

8.2 **Verify the real financial outcome** by reading state back from the source
of truth and comparing it against the intended effect.

8.3 **Never trust the agent's claim that an action succeeded.** The agent's
assertion carries zero weight and is not an input to the verification layer.

8.4 **A mismatch is escalated, never auto-remediated.** Do not issue a
compensating action against an unclear state — that turns one bad action into
two.

8.5 **An unverifiable outcome is `UNKNOWN`, not success.** Do not guess, and do
not let optimism become a default.

8.6 **Verification rejects; it does not repair.** It never rewrites the agent's
number to the correct one.

8.7 **Record every check performed, with inputs and results** — including the
checks that passed.

8.8 **Deferred outcomes are modelled as scheduled re-verification**, never as a
synchronous check that reports success prematurely.

---

## 9. Testing

9.1 **Test every financial calculation independently**, with hand-computed
known values, boundaries and undefined cases.

9.2 **Test every policy rule independently**, plus the composed engine.

9.3 **Test agent output against its schema** — accept valid, reject invalid.

9.4 **Include adversarial agent intents** as first-class tests: inflated amounts
(especially 100×, the rupee/paise error), fabricated evidence references,
out-of-scope targets, disallowed actions, negative/zero/absurd amounts, stale
evidence, scope widening, and confident-but-wrong reasoning.

9.5 **Include failure scenarios** — API failure, timeout, malformed payload,
invalid signature, policy error, audit-write failure.

9.6 **Include false alarms.** A test suite that only proves the agent detects
things does not prove it is safe. Restraint is tested as rigorously as
detection.

9.7 **Include duplicate events** — webhook redelivery, repeated detection,
concurrent identical intents.

9.8 **Include boundary conditions** — zero transactions, one transaction, all
success, all failure, one-paise amounts, very large amounts, exact-threshold
values, tie-breaking rounding.

9.9 **Tests are deterministic.** No network, no real LLM call, no wall-clock
dependence, no unseeded randomness, no test-order dependence. Inject time and
seeds.

9.10 **A test must never be weakened to make code pass.** Fix the code. If the
test is genuinely wrong, say so explicitly and explain why in the change.

9.11 **The core suite must run with the standard library alone:**

```bash
python3.11 -m unittest discover -s backend/tests -t . -v
```

9.12 **Never mock the thing under test.** Mock the boundary (Razorpay, LLM),
never the financial engine, the verifier, or the policy engine.

---

## 10. Code quality

10.1 **Small, cohesive modules.** One financial concept per module. If a module
needs "and" to describe it, split it.

10.2 **Avoid unnecessary abstractions.** No factory-of-factories, no plugin
registry with one plugin, no base class with one subclass, no dependency
injection framework. Write the function.

10.3 **No premature microservices, queues, caches or Kubernetes.** Modular
monolith (ADR-002).

10.4 **Clear, explicit naming.** `baseline_failure_rate`, not `calc`.
`excess_failed_transactions`, not `affected`. Money-carrying names state the
unit or use the `Money` type.

10.5 **Type every important interface.** All domain contracts, all public
function signatures in `financial/`, `policy/` and `verification/`. Contracts
validate themselves in `__post_init__`.

10.6 **Keep business logic out of route handlers.** A handler parses the
request, calls a service, and shapes the response. If a handler contains an
arithmetic operator on money, it is wrong.

10.7 **Domain contracts are immutable** (`frozen=True`) unless there is a
documented reason. Financial facts do not change after they are recorded.

10.8 **Respect the dependency direction:**
`domain ← financial ← data/detection/tools/policy/verification ← agent/execution ← api`.
Never import backwards. `domain` imports nothing internal.

10.9 **Secrets come from the environment only.** Never hardcode a credential,
never commit `.env`, never log a secret, never put one in an audit payload or an
LLM prompt. Add new variables to `.env.example` with empty values.

10.10 **No silent exception swallowing.** No bare `except:`, no `except:
pass`. Every caught exception is handled, logged, or re-raised — and near money,
handled means failing closed.

10.11 **No placeholder code that looks implemented.** A not-yet-built module
contains a docstring describing its contract and nothing else. Never a function
that returns a plausible fake value, and never a `TODO` that silently returns
`0`, `True`, or an empty list where a real value is expected.

10.12 **Comments explain why, not what.** Financial subtleties (why in-flight
payments are excluded, why a value is clamped) must be commented; obvious code
must not be.

---

## 11. Rules for AI coding agents

11.1 **Read ARCHITECTURE.md and PROJECT_RULES.md before modifying code.** Every
session. Not the summary — the files.

11.2 **Inspect before you write.** Find the existing implementation before
adding one. This codebase must have exactly one implementation of each concept.

11.3 **Never delete or overwrite existing work without a stated reason.** If
something looks wrong, say so and ask; do not quietly replace it.

11.4 **Follow the existing architecture.** Do not reorganise directories,
rename domain concepts, or restructure modules as a side effect of another task.

11.5 **Do not introduce a new framework, library or major dependency without
explicit justification and approval.** "It's convenient" is not justification.
Adding one to `domain/`, `financial/` or `data/` is forbidden outright (§4.8).

11.6 **Do not silently change an architectural decision.** ADRs in
ARCHITECTURE.md §21 are binding. Changing one requires saying so, explaining
why, and updating the ADR in the same change.

11.7 **Do not create a duplicate implementation** of a financial calculation,
a policy check, a contract, or a Razorpay call. Import the existing one.

11.8 **Explain significant architectural changes** before and after making
them, in plain language, including what you considered and rejected.

11.9 **Prefer small, testable, verifiable changes.** Land one coherent thing
with its tests rather than a large speculative structure.

11.10 **Update documentation in the same change as the architecture.**
Documentation drift is architectural drift.

11.11 **Run the test suite after every stage** and report the actual result. If
tests fail, say so and paste the output. Never report "done" for something you
did not verify.

11.12 **Never fabricate a Razorpay API detail.** If you are not certain, mark it
`REQUIRES OFFICIAL DOC VERIFICATION` and stop (§6.1).

11.13 **On architectural ambiguity, STOP and document it.** Add it to
ARCHITECTURE.md §22 and raise it. Do not silently make a major decision to keep
moving.

11.14 **Do not build ahead of the plan.** Building the agent, policy engine or
executor early — before their foundations are tested — produces exactly the
architectural drift this document exists to prevent. Check what stage the
project is on before adding a layer.

11.15 **State what you did not do.** Report intentional omissions explicitly.
Silence about a gap reads as completion.

11.16 **Do not weaken a rule, a test, or a guard to make progress.** Blocked is
a legitimate state; a disabled safety check is not. See Rule 0.

---

## 12. Pre-change checklist

Before proposing or committing any change:

- [ ] I have read ARCHITECTURE.md and PROJECT_RULES.md this session.
- [ ] No LLM output can reach financial state without verification + policy.
- [ ] No financial arithmetic exists outside `backend/financial/`.
- [ ] No `float` touches money or a rate.
- [ ] Undefined is represented as `None`, never `0`.
- [ ] Every new financial function has unit tests, including boundaries.
- [ ] Every new policy rule fails closed and is individually tested.
- [ ] No new third-party import in `domain/`, `financial/` or `data/`.
- [ ] No invented Razorpay endpoint, field, event or parameter.
- [ ] No duplicate implementation of an existing concept.
- [ ] No secret hardcoded, logged, or committed; `.env.example` updated.
- [ ] No placeholder that looks implemented but is not.
- [ ] Every consequential action is idempotent and audited.
- [ ] Documentation updated if the architecture changed.
- [ ] The test suite was actually run, and the real result is reported.
