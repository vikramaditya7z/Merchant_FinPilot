"""Synthetic financial data for development and evaluation.

Deterministic scenarios with known ground truth. Same seed, same data, every
time — which is what makes an assertion about a revenue figure meaningful.

Two things live here and must not be confused:

* **Data** — ``ScenarioDataset.agent_payments()`` / ``agent_enriched()``. Plain
  production contracts, no labels. This is what the engine and the agent see.
* **Labels** — ``GroundTruth``, ``SyntheticPayment.matched_anomaly``,
  ``planted_failure_ids()``. Evaluation only. Never in a production path, never
  in a prompt (PROJECT_RULES 2.7, ADR-005).

Standard library only, like ``domain`` and ``financial`` (ADR-001).
"""

from .generator import (
    DEFAULT_ANCHOR,
    DEFAULT_SEED,
    ScenarioDataset,
    SyntheticPayment,
    generate_all,
    generate_scenario,
)
from .ground_truth import GroundTruth, ScenarioId
from .scenarios import (
    BACKGROUND_FAILURES,
    PROVIDERS,
    REGIONS,
    SCENARIOS,
    SPARSE_MIX,
    STANDARD_MIX,
    Anomaly,
    MethodProfile,
    ScenarioSpec,
    all_scenario_ids,
    get_scenario,
    ground_truth_for,
    incident_scenario_ids,
    restraint_scenario_ids,
)

__all__ = [
    # identity & labels (evaluation only)
    "ScenarioId",
    "GroundTruth",
    "ground_truth_for",
    # specs
    "MethodProfile",
    "Anomaly",
    "ScenarioSpec",
    "SCENARIOS",
    "get_scenario",
    "all_scenario_ids",
    "incident_scenario_ids",
    "restraint_scenario_ids",
    "STANDARD_MIX",
    "SPARSE_MIX",
    "REGIONS",
    "PROVIDERS",
    "BACKGROUND_FAILURES",
    # generation
    "SyntheticPayment",
    "ScenarioDataset",
    "generate_scenario",
    "generate_all",
    "DEFAULT_SEED",
    "DEFAULT_ANCHOR",
]
