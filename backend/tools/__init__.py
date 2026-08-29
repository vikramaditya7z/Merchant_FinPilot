"""The Agent Tool Surface for Merchant FinPilot.

PROJECT_RULES 1.6, 4.2, 10.8 / ARCHITECTURE.md §9.

Tools are the LLM's only window onto the world, so the tool surface is the
security boundary.

Invariants
----------
1. **Read-only during investigation.** No tool available to the reasoning loop
   mutates state or calls a Razorpay write endpoint.
2. **Narrow and single-purpose.** Typed endpoints only; no generic queries,
   no code execution, no filesystem or network access.
3. **Explicit schemas.** Parameters are typed, bounded, and validated.
4. **Deterministic.** Financial numbers are computed by the deterministic engine;
   the LLM is never asked to calculate financial truth.
"""

from .contracts import (
    SliceSummary,
    TimeBucketSummary,
    ToolErrorCode,
    ToolResult,
)
from .incident_tools import (
    check_action_eligibility,
    get_baseline_comparison,
    get_failure_breakdown,
    get_incident_summary,
    get_revenue_exposure,
    get_time_series,
)
from .registry import BoundToolRegistry, ToolDefinition, ToolRegistry, create_default_registry

__all__ = [
    "SliceSummary",
    "TimeBucketSummary",
    "ToolErrorCode",
    "ToolResult",
    "ToolDefinition",
    "ToolRegistry",
    "BoundToolRegistry",
    "create_default_registry",
    "get_incident_summary",
    "get_failure_breakdown",
    "get_time_series",
    "get_baseline_comparison",
    "get_revenue_exposure",
    "check_action_eligibility",
]
