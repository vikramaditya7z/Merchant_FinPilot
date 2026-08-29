"""The HTTP API Layer — thin, typed application boundary.

PROJECT_RULES 1.6, 10.6-10.9 / ARCHITECTURE.md §1-§17.

Contract
--------
Deliberately thin. A handler validates its request, calls FinancialIncidentOrchestrator,
and shapes the response. Contains zero business logic, arithmetic on money, or policy logic.
"""

from .app import FinPilotApp, create_app
from .contracts import (
    ProcessIncidentRequest,
    ProcessIncidentResponse,
)
from .router import FinancialIncidentAPI

__all__ = [
    "FinancialIncidentAPI",
    "FinPilotApp",
    "create_app",
    "ProcessIncidentRequest",
    "ProcessIncidentResponse",
]
