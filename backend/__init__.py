"""Merchant FinPilot — AI Financial Autopilot for Merchants.

Core principle: LLMs reason. Deterministic systems verify.

Read ARCHITECTURE.md and PROJECT_RULES.md before modifying anything here.

Dependency direction (never import backwards):

    domain <- financial <- data / detection / tools / policy / verification
           <- agent / execution <- api

``domain``, ``financial`` and ``data`` import only the Python standard
library (ARCHITECTURE.md ADR-001).
"""

__version__ = "0.2.0"
