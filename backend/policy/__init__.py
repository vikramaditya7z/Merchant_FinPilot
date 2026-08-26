"""The Policy Engine — deterministic authorization.  **[Day 6 — not implemented]**

This package contains no implementation. It exists so the contract below is
recorded before any code is written against it (PROJECT_RULES 10.11).

Contract
--------
Runs **after** verification, on an already-verified intent, and never sees or asks
the LLM anything (ARCHITECTURE.md §11). Output is a
``domain.policy.PolicyDecision`` carrying a verdict in ``{ALLOW, BLOCK,
ESCALATE}``, every violation, any required approvals, and a rationale.

Verification asks "is this intent true?". Policy asks "is this intent
*permitted*?". They are separate questions and a correct answer to the first is
not an answer to the second — which is exactly the distinction the
``RECOVERY_NOT_ELIGIBLE`` scenario exists to catch: a genuine incident where
acting is still wrong.

Rule families
-------------
Kill switch (``FINPILOT_EXECUTION_ENABLED=false`` blocks everything) · mode guard
(non-test Razorpay mode blocks, for the MVP) · action allowlist · amount limits
(per action, per incident, daily aggregate) · rate limits · duplicate prevention
for the same ``(incident, action, target)`` · cooldown between actions on one
target · action-specific eligibility preconditions · evidence sufficiency
(minimum evidence count and significance) · confidence floor (below it,
``ESCALATE``, never ``ALLOW``) · blast radius (above an affected-entity count,
``ESCALATE``).

Design rules
------------
* **Fail closed.** Any error, missing input, unknown action, unparseable intent
  or unhandled case ⇒ not-``ALLOW``. The default is never permission
  (PROJECT_RULES 5.x).
* **Ambiguity escalates.** ``ESCALATE`` is a first-class and frequently correct
  outcome, not a failure mode. An engine that only ever allows or blocks is
  overconfident.
* **Every rule is an independent, individually testable pure function** of
  ``(verified_intent, context) → PolicyViolation | None``. No rule reads a clock,
  a config file, or a database directly; time and context are injected.
* **Collect all violations**, never short-circuit on the first, so the audit
  trail explains every reason the answer was no.
* **Data-in / decision-out.** No I/O, fully reproducible from an audit record.
  Rule versions are recorded with the decision so an old decision can be
  understood under the rules that produced it.
* A decision is **not a bearer token valid forever** — it has a short TTL, and
  the executor re-checks freshness (ARCHITECTURE.md §13).
* **No enrichment-only gating.** ``region`` and ``provider`` are inferred, not
  reported by Razorpay (ARCHITECTURE.md §12.2); no rule may turn solely on an
  enrichment-derived fact without an explicit documented note.

Limits and thresholds are configuration read from the environment, versioned, and
recorded — never literals scattered through rule bodies (PROJECT_RULES 10.9).

Dependencies: may import ``domain`` (and ``db`` for counters such as daily
aggregates and cooldowns). Must **not** import ``agent``, ``razorpay``,
``execution`` or ``verification`` (PROJECT_RULES 10.8).
"""
