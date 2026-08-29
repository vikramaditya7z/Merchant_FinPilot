"""Closed vocabularies for the investigation layer.

Distinguishes between factual observations, strong statistical evidence,
possible contributors, and insufficient evidence — without confusing
correlation with causal certainty.
"""

from enum import Enum


class EvidenceStrength(str, Enum):
    """Strength of empirical association between a dimension slice and the incident.

    * ``OBSERVED_FACT``: Directly computed, verified metrics without causal claim.
    * ``STRONG_EVIDENCE``: High concentration, material deviation, and valid
      statistical significance.
    * ``POSSIBLE_CONTRIBUTOR``: Moderate concentration or co-occurring deviation.
    * ``INSUFFICIENT_EVIDENCE``: Inadequate sample size, undefined baseline, or
      invalid statistical approximation.
    """

    OBSERVED_FACT = "observed_fact"
    STRONG_EVIDENCE = "strong_evidence"
    POSSIBLE_CONTRIBUTOR = "possible_contributor"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
