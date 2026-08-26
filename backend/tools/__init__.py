"""The agent's tool surface.  **[Day 5 — not implemented]**

This package contains no implementation. It exists so the contract below is
recorded before any code is written against it (PROJECT_RULES 10.11).

Contract
--------
Tools are the LLM's only window onto the world, so **the tool surface is the
security boundary** (ARCHITECTURE.md §9). Whatever is not exposed here, the agent
cannot reach.

Each tool is a thin adapter: validate arguments → call a deterministic function
in ``financial/`` → return a typed result. A tool never contains financial
arithmetic of its own, because a second implementation of a financial concept is
a second thing that can be wrong.

Invariants
----------
1. **Read-only during investigation.** No tool available to the reasoning loop
   mutates state or calls a Razorpay write endpoint. The only "write" the agent
   can perform is emitting an intent, which is a proposal.
2. **Narrow and single-purpose.** ``get_failure_breakdown(incident_id,
   dimension)``, never ``query(sql)``. No generic query tool, no code execution,
   no HTTP tool, no filesystem access.
3. **Explicit schemas.** Every parameter typed, bounded, and enumerated where
   possible. Free-text parameters are rejected unless there is a stated reason.
4. **Scoped ids.** Every id is validated against the incident's own scope, so
   the agent cannot pivot to another incident's or another merchant's data.
   Scope widening is privilege escalation.
5. **Every call audited** with its arguments and a digest of its result.
6. **Bounded.** Hard caps on calls per investigation, rows per result, and time
   window breadth.

Planned surface (all read-only), per ARCHITECTURE.md §9::

    get_incident_summary(incident_id)
    get_failure_breakdown(incident_id, dimension)   # method|reason|region|provider|hour
    get_time_series(incident_id, bucket)            # hourly|daily
    get_baseline_comparison(incident_id, dimension_value)
    get_sample_failed_payments(incident_id, limit)  # limit hard-capped
    get_revenue_exposure(incident_id)
    check_action_eligibility(incident_id, action)   # deterministic pre-check

``check_action_eligibility`` is the deliberate exception worth understanding: the
agent may *ask* whether an action would be eligible, and the answer is computed
deterministically. This lets it avoid proposing something obviously doomed
without giving it any authority over the answer.

Two traps specific to this layer
--------------------------------
* **Return typed results, not prose.** A tool that pre-summarises in English
  hands the agent a number nobody can re-derive.
* **Do not return a bare rate over an empty population.** ``None`` means
  undefined and must survive the trip to the model as "unknown", not as ``0``
  (ADR-004, PROJECT_RULES 1.7). Slice failure rates on failure-attribute
  dimensions are trivially 100% by construction — expose ``share_of_failures``
  instead, or the agent will confidently reason about a meaningless figure.
* **Never expose ground truth.** ``data.ground_truth`` is evaluation-only
  (PROJECT_RULES 2.7); no tool may read it, directly or transitively.

Dependencies: may import ``domain``, ``financial``, ``detection`` and ``db``.
Must not import ``agent``, ``execution`` or ``api`` (PROJECT_RULES 10.8).
"""
