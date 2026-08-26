"""Verification contracts.

Used for both verification passes (ARCHITECTURE.md 10, 14):

* **pre-execution** — is the agent's intent correct and safe?
* **post-execution** — did the intended thing actually happen in reality?

The governing rule: verification *rejects*, it does not repair
(PROJECT_RULES 8.6). And an outcome we could not establish is ``INCONCLUSIVE``,
never success (PROJECT_RULES 8.5).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from .enums import VerificationPhase, VerificationStatus
from .errors import DomainValidationError
from .window import require_utc


@dataclass(frozen=True)
class VerificationCheck:
    """One deterministic check, with what was expected and what was seen.

    ``passed=None`` means inconclusive — the check could not be evaluated. That
    is deliberately distinct from ``False``: "we could not tell" and "it is
    wrong" call for different responses.

    ``expected`` and ``observed`` are text so any check is auditable and
    human-readable regardless of the types involved.
    """

    check_id: str
    name: str
    passed: Optional[bool]
    expected: str
    observed: str
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("check_id", "name", "expected", "observed"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"VerificationCheck.{name} must be non-empty")
        if self.passed is not None and not isinstance(self.passed, bool):
            raise DomainValidationError("VerificationCheck.passed must be bool or None")

    @property
    def is_inconclusive(self) -> bool:
        return self.passed is None

    def __str__(self) -> str:
        mark = "?" if self.passed is None else ("pass" if self.passed else "FAIL")
        return f"[{mark}] {self.name}: expected {self.expected}, observed {self.observed}"


@dataclass(frozen=True)
class VerificationResult:
    """The outcome of a verification pass.

    Every check is recorded, including the ones that passed
    (PROJECT_RULES 8.7), so a reviewer can see what was actually examined rather
    than only what went wrong.
    """

    verification_id: str
    phase: VerificationPhase
    subject_id: str
    status: VerificationStatus
    verified_at: datetime
    checks: Tuple[VerificationCheck, ...] = ()
    summary: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("verification_id", "subject_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"VerificationResult.{name} must be non-empty")
        if not isinstance(self.phase, VerificationPhase):
            raise DomainValidationError(f"invalid VerificationPhase: {self.phase!r}")
        if not isinstance(self.status, VerificationStatus):
            raise DomainValidationError(f"invalid VerificationStatus: {self.status!r}")
        object.__setattr__(
            self, "verified_at", require_utc(self.verified_at, "VerificationResult.verified_at")
        )
        if not isinstance(self.checks, tuple):
            raise DomainValidationError("VerificationResult.checks must be a tuple")
        seen = set()
        for check in self.checks:
            if not isinstance(check, VerificationCheck):
                raise DomainValidationError("checks must contain VerificationCheck")
            if check.check_id in seen:
                raise DomainValidationError(f"duplicate check_id: {check.check_id}")
            seen.add(check.check_id)

        # VERIFIED is only permitted when every check actually passed. Without
        # this, an inconclusive check could be quietly rounded up to success.
        if self.status is VerificationStatus.VERIFIED:
            if not self.checks:
                raise DomainValidationError("VERIFIED requires at least one check")
            for check in self.checks:
                if check.passed is not True:
                    raise DomainValidationError(
                        f"cannot be VERIFIED while check {check.check_id!r} is "
                        f"{'inconclusive' if check.is_inconclusive else 'failed'}"
                    )
        if self.status in (VerificationStatus.MISMATCH, VerificationStatus.REJECTED):
            if not any(check.passed is False for check in self.checks):
                raise DomainValidationError(
                    f"{self.status.value} requires at least one failed check"
                )

    @property
    def is_verified(self) -> bool:
        return self.status is VerificationStatus.VERIFIED

    @property
    def failed_checks(self) -> Tuple[VerificationCheck, ...]:
        return tuple(check for check in self.checks if check.passed is False)

    @property
    def inconclusive_checks(self) -> Tuple[VerificationCheck, ...]:
        return tuple(check for check in self.checks if check.passed is None)

    def __str__(self) -> str:
        return (
            f"VerificationResult({self.phase.value}, {self.status.value}, "
            f"{len(self.checks)} checks)"
        )
