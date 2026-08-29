"""Execution storage and idempotency tracking.

PROJECT_RULES 7.3, 7.4 / ARCHITECTURE.md §13, §15.

Ensures execution records are persisted under unique idempotency keys,
preventing duplicate actions on identical triggers.
"""

from typing import Dict, List, Optional

from .contracts import ExecutionResult


class ExecutionStore:
    """In-memory idempotency and execution result store."""

    def __init__(self) -> None:
        self._results_by_key: Dict[str, ExecutionResult] = {}
        self._results_list: List[ExecutionResult] = []

    def get(self, idempotency_key: str) -> Optional[ExecutionResult]:
        """Fetch previously recorded execution result by idempotency key."""
        return self._results_by_key.get(idempotency_key)

    def save(self, result: ExecutionResult) -> None:
        """Store an execution result under its idempotency key."""
        self._results_by_key[result.idempotency_key] = result
        self._results_list.append(result)

    def has_key(self, idempotency_key: str) -> bool:
        """Check if an idempotency key has already been recorded."""
        return idempotency_key in self._results_by_key

    def list_results(self) -> List[ExecutionResult]:
        """Return all recorded execution results."""
        return list(self._results_list)

    def count(self) -> int:
        """Return count of recorded execution results."""
        return len(self._results_list)
