"""Deterministic failure investigation layer.

PROJECT_RULES 3.5 / ARCHITECTURE.md §8.

Examines opened incidents across dimensions (payment method, geography/region,
provider/route, failure code, failure category, hour of day).

Distinguishes between observed facts, strong statistical evidence, possible
contributors, and insufficient evidence without confusing correlation with
causal certainty.
"""

from .analyzer import INVESTIGATED_DIMENSIONS, analyze_incident
from .enums import EvidenceStrength
from .findings import DimensionalFinding, InvestigationReport
from .investigator import Investigator, investigate_incident

__all__ = [
    "EvidenceStrength",
    "DimensionalFinding",
    "InvestigationReport",
    "INVESTIGATED_DIMENSIONS",
    "analyze_incident",
    "Investigator",
    "investigate_incident",
]
