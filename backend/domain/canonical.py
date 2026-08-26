"""Canonical serialization and digests.

An idempotency key built from an unstable serialization is not an idempotency
key (PROJECT_RULES 7.4). Every hash in this system — intent content hash,
execution key, audit payload digest — goes through here.

Guarantees:

* Deterministic key ordering.
* No floats. A float in a hashed payload makes the hash platform-sensitive, and
  a float in a financial payload is a defect anyway (PROJECT_RULES 1.6).
* Explicit, lossless encoding of ``Money``, ``Decimal``, ``Enum``, ``datetime``.
* Refusal on anything it cannot represent exactly.
"""

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from .errors import NonCanonicalValueError
from .money import Money

# Substrings that suggest a credential. Checked on audit payload keys so a
# secret cannot be recorded by accident (PROJECT_RULES 6.10).
SECRET_KEY_HINTS = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "signature",
    "private_key",
    "credential",
    "key_secret",
)


def canonicalize(value: Any) -> Any:
    """Reduce ``value`` to JSON-safe primitives, losslessly and deterministically."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        # Before int: bool is a subclass of int.
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise NonCanonicalValueError(
            "floats cannot be canonically serialized; use int minor units or Decimal"
        )
    if isinstance(value, Money):
        return {
            "__type__": "money",
            "minor_units": value.minor_units,
            "currency": value.currency.value,
        }
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise NonCanonicalValueError(f"non-finite Decimal: {value}")
        # A normalized string keeps 0.50 and 0.5 hashing identically.
        return {"__type__": "decimal", "value": _normalized_decimal_str(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise NonCanonicalValueError("datetime must be timezone-aware")
        return {"__type__": "datetime", "value": value.isoformat()}
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise NonCanonicalValueError(f"mapping keys must be str, got {key!r}")
            out[key] = canonicalize(item)
        return dict(sorted(out.items()))
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        # Sorted by canonical form so set ordering cannot affect the digest.
        return sorted((canonical_json(item) for item in value))
    raise NonCanonicalValueError(
        f"cannot canonically serialize {type(value).__name__}"
    )


def _normalized_decimal_str(value: Decimal) -> str:
    """Stable decimal text: no exponent notation, no trailing-zero ambiguity."""
    normalized = value.normalize()
    sign, digits, exponent = normalized.as_tuple()
    if isinstance(exponent, int) and exponent > 0:
        # normalize() turns 100 into 1E+2; expand it back.
        normalized = normalized.quantize(Decimal(1))
    return format(normalized, "f")


def canonical_json(value: Any) -> str:
    """Canonical JSON text for ``value``."""
    return json.dumps(
        canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value: Any) -> str:
    """SHA-256 hex digest of the canonical form."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def short_digest(value: Any, length: int = 16) -> str:
    """Truncated digest, for human-readable keys. Not for integrity checks."""
    if length < 8:
        raise NonCanonicalValueError("short_digest length must be >= 8")
    return digest(value)[:length]


def assert_no_secrets(payload: Mapping[str, Any]) -> None:
    """Raise if any key looks like a credential.

    A heuristic, not a security boundary — the real control is never passing
    secrets in. This catches the accident.
    """
    from .errors import SecretLeakError

    for key in payload:
        lowered = str(key).lower()
        for hint in SECRET_KEY_HINTS:
            if hint in lowered:
                raise SecretLeakError(
                    f"payload key {key!r} looks like a credential and must not be recorded"
                )
