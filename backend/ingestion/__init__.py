"""The Payment Ingestion & Normalization Layer for Merchant FinPilot.

PROJECT_RULES 2.3, 2.6, 2.8, 10.7 / ARCHITECTURE.md §12.

Converts raw, external payment events and batch dictionaries into canonical domain
contracts (``Payment`` and ``PaymentEnrichment``), persists them via the Database
repository, and records auditable ingestion events.
"""

from .enricher import PaymentEnricher
from .normalizer import PaymentNormalizer
from .service import IngestionItemResult, IngestionResult, IngestionService

__all__ = [
    "PaymentNormalizer",
    "PaymentEnricher",
    "IngestionService",
    "IngestionResult",
    "IngestionItemResult",
]
