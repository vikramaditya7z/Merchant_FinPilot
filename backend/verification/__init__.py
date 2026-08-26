"""Financial Verifier and post-action outcome verification.
**[Day 6 — not implemented]**

This package contains no implementation. It exists so the contract below is
recorded before any code is written against it (PROJECT_RULES 10.11).

Two verifications live here. Both are deterministic, both are mandatory, and
they answer different questions.

1. Pre-execution — is the intent correct and safe?
--------------------------------------------------
An **independent re-derivation** of the agent's claims from source records. Its
design premise is that the agent may be wrong, stale, or adversarial
(ARCHITECTURE.md §10). Input: ``AgentIntent`` + incident. Output:
``domain.verification.VerificationResult``.

Checks, each paired with the failure mode it defends against:

* Recompute every numeric claim from source records → hallucinated or
  miscalculated amounts.
* Evidence references resolve **and support the claim** → fabricated citations.
* Evidence is fresh within a maximum staleness window → acting on facts that
  have since changed (see ``FinancialEvidence.is_fresh_at``).
* Amounts within tolerance of re-derived values → silent drift, and the
  off-by-100 rupee/paise error.
* Target entity exists in Razorpay → invented payment and order ids.
* Target entity state permits the action → refunding an uncaptured payment.
* Currency matches and is INR → currency confusion.
* Intent schema and enum membership → out-of-vocabulary actions.
* Intent scope ⊆ incident scope → privilege escalation by scope widening.

**Tolerance policy.** Comparison is exact (``==`` on integer paise) for money
that must match a known record. A tolerance is permitted only for derived
aggregate estimates, must be expressed as an explicit basis-point bound, and is
recorded in the ``VerificationResult``. Verification is not a rounding fixer: it
does not correct the agent's number, it **rejects** it.

This layer is the reason the LLM can be treated as untrusted input.

2. Post-execution — did the intended thing actually happen?
-----------------------------------------------------------
Five checks in order (ARCHITECTURE.md §14): was the response well-formed and
successful; read the real entity state back from Razorpay; does observed state
match the intended effect; do the financial numbers still reconcile; and was
there any unintended side effect such as a duplicate or a wrong amount.

* A ``2xx`` response is **not** proof of a financial effect. Only reading state
  back is.
* The agent's assertion that an action succeeded carries **zero** weight and is
  not an input here.
* Mismatch ⇒ ``MISMATCH`` + escalate + audit. **Never auto-remediate a
  mismatch:** a compensating action on top of an unclear state is how one bad
  action becomes two.
* Unverifiable outcome ⇒ ``UNKNOWN`` + escalate. Do not guess.
* An outcome that only resolves later (whether a customer actually paid a link)
  is modelled as a **scheduled re-verification**, not a synchronous check that
  lies.

Obligations for both
--------------------
Pure, data-in/result-out, no clock reads (time is injected), fully reproducible
from an audit record. Verification must not call the LLM for any part of any
check — including "does this evidence support this claim?".

Dependencies: may import ``domain``, ``financial``, ``razorpay`` (read paths
only) and ``db``. Must not import ``agent``, ``policy`` or ``execution``
(PROJECT_RULES 10.8).
"""
