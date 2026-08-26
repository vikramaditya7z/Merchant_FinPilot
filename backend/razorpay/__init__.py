"""Razorpay integration boundary.  **[Day 4 — not implemented]**

This package contains no implementation. It exists so the contract below is
recorded before any code is written against it (PROJECT_RULES 10.11).

Contract
--------
The **only** place in the codebase that talks to Razorpay. No other package
imports the SDK, holds a credential, or knows a URL; everything else speaks
``domain`` contracts (ARCHITECTURE.md §12).

Inbound (webhooks): verify the signature → reject anything unverified → parse →
map to domain contracts → persist the raw payload for audit → hand off. Unknown
fields are preserved in the raw record, never silently dropped
(ARCHITECTURE.md §12.3).

Outbound: accept a **fully authorized** action, attach an idempotency key, make
exactly one call to exactly one documented endpoint, and return the raw response
for independent verification. The adapter contains no policy logic and no
financial arithmetic — it does not decide, and it does not compute.

The standing rule on API surface
--------------------------------
**An endpoint, parameter, field or event name that has not been read in official
Razorpay documentation does not exist** (PROJECT_RULES §6,
ARCHITECTURE.md §12.1). Do not infer one from a blog post, an SDK example, a
model's recollection, or this docstring.

ARCHITECTURE.md §12.1 records the verification status of every capability this
product needs. Several are marked ``TBD`` or ``REQUIRES OFFICIAL DOC
VERIFICATION`` and must be treated as **unavailable** until the table is updated
against official docs — including idempotency-key header support, programmatic
retry of a failed payment, any runtime control of routing or method
availability, aggregate analytics endpoints, rate limits, and per-action
test-mode support. Because provider-side idempotency is unverified, our own
idempotency layer must be sufficient on its own (ARCHITECTURE.md §15).

``region``/``segment`` and ``provider``/``route`` are **not** payment fields. They
are internal ``PaymentEnrichment`` (ARCHITECTURE.md §12.2), which is why
``domain.payment`` keeps them off ``Payment``.

Obligations
-----------
* **Credentials from the environment only**, never hardcoded, never logged,
  never in an audit payload or an LLM prompt (PROJECT_RULES 10.9).
* **HMAC verification over the raw request body**, before parsing, using a
  constant-time comparison. A webhook that fails verification is not a webhook.
* **Redelivery is expected, not exceptional.** Handlers are idempotent and
  deduplicate on event id.
* **A 2xx is not proof of a financial effect.** Reading state back is
  (ARCHITECTURE.md §14).
* **Never retry a consequential action on an ambiguous outcome.** A timeout is
  not a failure: the action may have succeeded. Record ``UNKNOWN`` and escalate
  (PROJECT_RULES §7).
* **Mode guard.** During the MVP, write actions are permitted in test mode only;
  the enforcement lives in ``policy``, but this package must surface the mode
  honestly rather than defaulting it.

Dependencies: may import ``domain``. Must not import ``agent``, ``policy``,
``verification`` or ``detection`` (PROJECT_RULES 10.8).

Naming hazard
-------------
This package shares its name with Razorpay's own PyPI distribution. Absolute
imports are the default in Python 3, so ``import razorpay`` inside this package
resolves to the installed SDK and not to itself — correct, but only while
``backend/`` is never placed on ``sys.path`` (the suite runs with
``PYTHONPATH=.`` from the repository root, so it isn't). The name is prescribed
by ARCHITECTURE.md §6.1 and is left as-is rather than changed unilaterally; if
the SDK is adopted on Day 4 and the collision bites, renaming this package to
``razorpay_gateway`` is the fix, and it requires an ARCHITECTURE.md amendment.
"""
