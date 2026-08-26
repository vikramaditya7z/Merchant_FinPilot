"""The LLM reasoning loop.  **[Day 5 — not implemented]**

This package contains no implementation. It exists so the contract below is
recorded before any code is written against it (PROJECT_RULES 10.11).

Contract
--------
A bounded loop over read-only tools that ends in a **proposal**. Its only output
is a ``domain.intent.AgentIntent`` — a description of what it wants to happen,
which it has no authority to make happen (ARCHITECTURE.md §8).

The loop owns steps 2–5 and step 10 of the workflow: orient on the incident,
investigate by choosing which dimension to slice next, form a root-cause
hypothesis citing evidence ids, propose exactly one intent, and later narrate the
outcome. Steps 1, 6, 7, 8 and 9 — detection, verification, authorization,
execution, confirmation — contain **no LLM call whatsoever**.

What the agent does and does not do
-----------------------------------
* It chooses **what to look at**. It never computes (PROJECT_RULES 1.2). Every
  number it reasons about was produced by ``financial/`` and reaches it through a
  tool result.
* It does not decide that an incident exists (PROJECT_RULES 3.11).
* It holds **no client, no session, no credential, and no reference to the
  executor** (ARCHITECTURE.md §8.1). This is what makes the loop a pure function
  from evidence to proposal, replayable against a recorded incident in tests with
  no possibility of a side effect.
* A claim with no resolvable ``evidence_ref`` is rejected at validation, not
  argued with.
* Its assertion that an action succeeded carries zero weight and is not an input
  to the verification layer (ARCHITECTURE.md §14).

Obligations
-----------
* **Bounded.** Hard caps on iterations, tool calls, tokens and wall-clock. An
  unbounded investigation is both a cost and a correctness risk.
* **Treat model output as untrusted input.** Parse against a schema; on a
  malformed intent, allow exactly one bounded reprompt and then escalate. Never
  coerce a malformed intent into a valid one (ARCHITECTURE.md §17).
* **Never put a secret or a ground-truth label in a prompt** (PROJECT_RULES 2.7,
  10.9). ``data.ground_truth`` exists for evaluation only and must not be
  reachable from any prompt-building path.
* **Record everything.** The full reasoning trace, model id and prompt version go
  to the audit trail; a decision that cannot be replayed cannot be defended.
* **Degrading is allowed.** If the LLM is unavailable, slow, or over budget, the
  incident stays open and undiagnosed and no action is taken. Deterministic
  detection and metrics still work — the system is useful without the LLM
  (ARCHITECTURE.md §17).

Dependencies: may import ``domain``, ``tools`` and ``audit``. Must **not** import
``razorpay``, ``execution`` or ``db`` directly — the whole design rests on the
reasoning loop having no path to mutable financial state (PROJECT_RULES 10.8,
ARCHITECTURE.md §8.1).
"""
