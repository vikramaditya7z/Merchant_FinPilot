"""HTTP surface.  **[Day 8 — not implemented]**

This package contains no implementation. It exists so the contract below is
recorded before any code is written against it (PROJECT_RULES 10.11).

Contract
--------
Deliberately thin. A handler validates its request, calls one service, and shapes
the response. **If a handler contains an arithmetic operator on money, it is
wrong** (PROJECT_RULES 10.6).

Endpoints planned for Day 8: list and read incidents, read an incident's evidence
and metrics, read the intent and the policy decision, read the audit trail for an
incident, and the human approval/rejection endpoint for escalated intents. Plus
the inbound Razorpay webhook route, which does nothing but hand the raw body to
``razorpay`` for signature verification — it must not parse before verifying.

Obligations
-----------
* **Never trust a financial value from the client.** Amounts, rates, counts and
  window bounds are read from the backend, Razorpay, or recomputed — never
  accepted from a request body (PROJECT_RULES §2). A request may name a target;
  it may not state what the target is worth.
* **Never expose a route that executes an action directly.** Execution follows
  verification and authorization or it does not happen (PROJECT_RULES 1.5).
  Human approval of an escalated intent re-enters the pipeline at the policy
  gate; it does not skip to the executor.
* **Never serve ground-truth labels.** ``data.ground_truth`` is evaluation-only
  (PROJECT_RULES 2.7).
* **Never leak a secret or a raw credential** in a response, a log line or an
  error body (PROJECT_RULES 10.9). Internal exception detail does not go over the
  wire.
* **Serialize money as integer minor units plus an explicit currency**, and rates
  as exact decimal strings. Emitting a JSON float for an amount reintroduces
  precisely the representation this codebase avoids (PROJECT_RULES 4.2).
* **Preserve the None/zero distinction across the boundary.** An undefined rate
  serializes as ``null``, never as ``0`` (ADR-004). Rendering "0% failure rate"
  for "no data" is a lie the frontend cannot detect.
* **Validate every external input** and reject rather than coerce.

Dependencies: the top of the stack — may import anything below it. Nothing may
import ``api`` (PROJECT_RULES 10.8).
"""
