"""The executor.  **[Day 7 — not implemented]**

This package contains no implementation. It exists so the contract below is
recorded before any code is written against it (PROJECT_RULES 10.11).

Contract
--------
**The executor is deliberately stupid.** Intelligence here is a security bug: it
becomes a second, untested authorization path (ARCHITECTURE.md §13).

It accepts only an authorized action carrying a valid ``PolicyDecision(ALLOW)``,
re-checks that decision's integrity and freshness (a decision is not a bearer
token valid forever — it has a short TTL), attaches an idempotency key derived
deterministically from ``(incident_id, action, target, canonical_parameters)``,
performs **exactly one** bounded outbound call, and records a
``domain.execution.ActionResult`` — including on failure, including on timeout.

What it must not do
-------------------
No policy checks. No amount computation. No "the amount looks wrong so I'll fix
it". No automatic retry loop. No "that action failed, try a different one". No
fallback of any kind. It never decides, never computes, never interprets.

Ambiguity
---------
**Never retry a consequential action on an ambiguous outcome.** A timeout is not
a failure: the action may have succeeded. Record ``UNKNOWN`` and escalate
(PROJECT_RULES §7, ARCHITECTURE.md §13).

Idempotency
-----------
The ``execution_key`` is persisted with a unique constraint **before** the
outbound call, so a crash mid-call cannot produce a second attempt. A
pre-existing key short-circuits and returns the recorded result rather than
acting again. The key is built over a canonical serialization
(``domain.canonical``) — sorted keys, no floats, fixed encoding — because an
unstable hash is not an idempotency key (ARCHITECTURE.md §15).

Razorpay-side idempotency support is unverified (ARCHITECTURE.md §12.1), so this
layer must be sufficient on its own. Provider support, once confirmed, is defence
in depth and nothing more.

Every consequential action is recorded before and after the call. An unrecorded
action did not happen, which is the worst possible state for one that did.

Dependencies: may import ``domain``, ``razorpay``, ``db`` and ``audit``. Must
**not** import ``policy``, ``verification``, ``agent`` or ``financial``: it does
not re-decide, it does not re-verify, and it has no reason to do arithmetic
(PROJECT_RULES 10.8).
"""
