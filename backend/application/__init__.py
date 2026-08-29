"""Application layer — orchestrates FinPilot pipeline components.

PROJECT_RULES 1.4 / ARCHITECTURE.md §1-§15.

Contract
--------
Coordinates the deterministic and reasoning components in order:
Detection -> Investigation -> Agent -> Verifier -> Policy -> Execution.
"""

from .contracts import PipelineResult, PipelineStage, PipelineStatus
from .orchestrator import FinancialIncidentOrchestrator

__all__ = [
    "FinancialIncidentOrchestrator",
    "PipelineResult",
    "PipelineStage",
    "PipelineStatus",
]
