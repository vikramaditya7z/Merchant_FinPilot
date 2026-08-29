"""Contracts, result containers, and error types for the Agent Tool Surface.

PROJECT_RULES 1.6, 4.2, 10.8 / ARCHITECTURE.md §9.

Guarantees:
- Strictly typed input and output structures.
- All monetary amounts in integer paise (never float).
- No undefined-to-zero coercions.
- Explicit error codes for expected failure modes.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class ToolErrorCode(str, Enum):
    """Error codes returned by tool execution."""
    NOT_FOUND = "NOT_FOUND"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ToolResult:
    """Standard container for tool execution responses."""
    success: bool
    data: Optional[Dict[str, Any]] = None
    error_code: Optional[ToolErrorCode] = None
    error_message: Optional[str] = None

    @classmethod
    def ok(cls, data: Dict[str, Any]) -> "ToolResult":
        return cls(success=True, data=data)

    @classmethod
    def error(cls, code: ToolErrorCode, message: str) -> "ToolResult":
        return cls(success=False, error_code=code, error_message=message)

    def to_dict(self) -> Dict[str, Any]:
        """Convert result to a clean dictionary for LLM consumption."""
        if self.success:
            return {"status": "SUCCESS", "data": self.data}
        return {
            "status": "ERROR",
            "error_code": self.error_code.value if self.error_code else None,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class SliceSummary:
    """Structured summary of a single dimensional breakdown slice."""
    dimension: str
    value: str
    total_count: int
    succeeded_count: int
    failed_count: int
    failed_gmv_paise: int
    source_confidence: str
    share_of_failures: str
    failure_rate_percent: Optional[str] = None
    baseline_rate_percent: Optional[str] = None
    deviation_percentage_points: Optional[str] = None
    relative_lift: Optional[str] = None
    evidence_strength: Optional[str] = None


@dataclass(frozen=True)
class TimeBucketSummary:
    """Summary of traffic and failures within a time interval."""
    start: str
    end: str
    total_transactions: int
    succeeded: int
    failed: int
    undecided: int
    failure_rate_percent: Optional[str]
    failed_gmv_paise: int
