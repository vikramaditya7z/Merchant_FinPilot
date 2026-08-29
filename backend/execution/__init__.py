"""The Execution Layer — deterministic, fail-closed action dispatch.

PROJECT_RULES 1.4, 7.1-7.8 / ARCHITECTURE.md §13.

Contract
--------
Executes ONLY actions authorized by a valid, unexpired PolicyDecision(ALLOW).
Enforces exact intent hash matching and deterministic idempotency deduplication.
Dispatches to pluggable adapters with explicit simulation status tagging.
"""

from .adapters import ExecutionAdapter, SimulatedExecutionAdapter
from .contracts import ExecutionRequest, ExecutionResult
from .engine import ExecutionEngine
from .store import ExecutionStore

__all__ = [
    "ExecutionEngine",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionAdapter",
    "SimulatedExecutionAdapter",
    "ExecutionStore",
]
