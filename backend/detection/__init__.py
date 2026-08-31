"""Deterministic incident opening.

PROJECT_RULES 3.11, 4.9 / ARCHITECTURE.md §8 (ADR-006).

Turns a ``FinancialMetrics`` into a decision: *does an incident exist?* Input is
the output of ``financial.engine.compute_metrics``; output is a
``domain.incident.FinancialIncident`` or ``None``.

No LLM call happens here — the agent never decides that an incident exists
(PROJECT_RULES 3.11, ARCHITECTURE.md §8 step 1).

**This is where thresholds live.** ``financial/`` measures; ``detection/``
judges. That split is ADR-006 and PROJECT_RULES 4.9: the z-test in
``financial.significance`` answers "could this be noise?", and only this package
answers "does this open an incident?". Thresholds are configuration, versioned
and recorded in the audit trail, never constants buried in the arithmetic.

Obligations
-----------
* **Deduplicate.** Detection runs repeatedly over overlapping windows. Identity
  comes from ``FinancialIncident.incident_key``, so the same degradation is
  recognised rather than re-opened every poll (ARCHITECTURE.md §15).
* **Respect insufficient data.** When ``FinancialMetrics`` carries no baseline
  or no deviation, the answer is "cannot tell", never "healthy"
  (PROJECT_RULES 1.7, ADR-004).
* **Do not trust the normal approximation blindly.** A confident p-value on thin
  data is still inadmissible; gate on
  ``SignificanceResult.normal_approximation_valid`` as well as ``p_value``.
* **Be silent when nothing is wrong.** Measured against the scenario set,
  ``FALSE_ALARM`` and ``SMALL_RANDOM_VARIATION`` must not open an incident, and
  ``INSUFFICIENT_DATA`` must abstain (ARCHITECTURE.md §19). Restraint is graded
  as heavily as detection.

Dependencies: may import ``domain`` and ``financial``. Must not import
``agent``, ``execution`` or ``api`` (PROJECT_RULES 10.8).
"""

from .config import DEFAULT_RULE_VERSION, DetectionConfig
from .detector import Detector, detect_from_payments, detect_incident
from .evaluator import (
    DetectionEvaluation,
    DetectionReason,
    determine_severity,
    evaluate_metrics,
)
from .live_evaluator import LiveEvaluationResult, LiveWindowEvaluator

__all__ = [
    # Configuration
    "DetectionConfig",
    "DEFAULT_RULE_VERSION",
    # Evaluation
    "DetectionEvaluation",
    "DetectionReason",
    "evaluate_metrics",
    "determine_severity",
    # Detection
    "Detector",
    "detect_incident",
    "detect_from_payments",
    # Live Evaluator
    "LiveWindowEvaluator",
    "LiveEvaluationResult",
]
