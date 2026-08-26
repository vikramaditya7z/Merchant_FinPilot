"""Errors raised by the Merchant FinPilot domain layer.

Every error here signals *refusal*, never a degraded best guess. Near money,
failing loudly is safer than proceeding with a plausible value.
"""


class FinPilotError(Exception):
    """Base class for all Merchant FinPilot errors."""


class DomainValidationError(FinPilotError, ValueError):
    """A domain contract was constructed with invalid data.

    Raised from ``__post_init__`` validators. Contracts validate themselves so
    an invalid financial fact cannot exist in memory in the first place.
    """


class CurrencyMismatchError(DomainValidationError):
    """Arithmetic was attempted across two different currencies."""


class MoneyPrecisionError(DomainValidationError):
    """A float, or another lossy type, was used where money was required.

    Money is an exact integer count of minor units. See PROJECT_RULES 1.6.
    """


class InsufficientDataError(FinPilotError):
    """A calculation was requested over a population too small to support it.

    Callers that can represent "unknown" should prefer an ``Optional`` return
    over catching this. See PROJECT_RULES 1.7: undefined is not zero.
    """


class NonCanonicalValueError(DomainValidationError):
    """A value cannot be canonically serialized for hashing or auditing.

    Unstable serialization means unstable idempotency keys. See
    PROJECT_RULES 7.4.
    """


class SecretLeakError(FinPilotError):
    """A payload appeared to contain a credential.

    Audit payloads and prompts must never carry secrets. See
    PROJECT_RULES 6.10.
    """
