"""Money — an exact integer count of minor units.

PROJECT_RULES 1.6 / ARCHITECTURE.md 7.1 (ADR-003).

Money is an ``int`` number of paise, never a float and never a rupee-denominated
number in internal code. This mirrors Razorpay's own representation (the payment
entity's ``amount`` is an integer in the smallest currency unit), so no
conversion happens at the boundary and an entire class of precision bug cannot
occur.

``float`` is rejected at construction. It is not rounded, coerced, or accepted
with a warning: a float amount means the caller lost precision somewhere
upstream, and that is a defect worth surfacing.
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Union

from .enums import Currency
from .errors import CurrencyMismatchError, DomainValidationError, MoneyPrecisionError

# The single rounding mode used anywhere money is produced from a ratio.
MONEY_ROUNDING = ROUND_HALF_UP


@dataclass(frozen=True, order=False)
class Money:
    """An exact monetary amount.

    Attributes:
        minor_units: Whole paise. May be zero or negative (a delta), but
            contracts that require a positive amount validate that themselves.
        currency: Explicit. Mixed-currency arithmetic raises.
    """

    minor_units: int
    currency: Currency = Currency.INR

    def __post_init__(self) -> None:
        # bool is a subclass of int; True would silently become 1 paisa.
        if isinstance(self.minor_units, bool):
            raise MoneyPrecisionError("Money.minor_units must be an int, not a bool")
        if isinstance(self.minor_units, float):
            raise MoneyPrecisionError(
                "Money.minor_units must be an int count of paise, not a float. "
                "Use Money.from_rupees() for a decimal rupee value."
            )
        if not isinstance(self.minor_units, int):
            raise MoneyPrecisionError(
                f"Money.minor_units must be an int, got {type(self.minor_units).__name__}"
            )
        if not isinstance(self.currency, Currency):
            raise DomainValidationError(
                f"Money.currency must be a Currency, got {self.currency!r}"
            )

    # -- constructors -------------------------------------------------------

    @classmethod
    def zero(cls, currency: Currency = Currency.INR) -> "Money":
        return cls(0, currency)

    @classmethod
    def from_paise(cls, paise: int, currency: Currency = Currency.INR) -> "Money":
        return cls(paise, currency)

    @classmethod
    def from_rupees(
        cls, rupees: Union[int, str, Decimal], currency: Currency = Currency.INR
    ) -> "Money":
        """Build from a major-unit value. For tests, fixtures and display input.

        Accepts ``int``, ``str`` or ``Decimal`` — never ``float``. A value with
        sub-paise precision is rejected rather than rounded, because silently
        dropping a fraction of a currency unit is how reconciliation breaks.
        """
        if isinstance(rupees, bool) or isinstance(rupees, float):
            raise MoneyPrecisionError(
                "from_rupees() does not accept float; pass a str or Decimal"
            )
        try:
            value = Decimal(rupees)
        except (ArithmeticError, ValueError) as exc:
            raise MoneyPrecisionError(f"not a valid decimal amount: {rupees!r}") from exc
        scaled = value * currency.minor_units_per_unit
        if scaled != scaled.to_integral_value():
            raise MoneyPrecisionError(
                f"{rupees} {currency.value} is not a whole number of minor units"
            )
        return cls(int(scaled), currency)

    # -- conversion ---------------------------------------------------------

    def as_rupees(self) -> Decimal:
        """Exact major-unit value. For display and reporting only."""
        return Decimal(self.minor_units) / Decimal(self.currency.minor_units_per_unit)

    # -- arithmetic ---------------------------------------------------------

    def _require_same_currency(self, other: "Money") -> None:
        if self.currency is not other.currency:
            raise CurrencyMismatchError(
                f"cannot combine {self.currency.value} and {other.currency.value}"
            )

    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.minor_units + other.minor_units, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.minor_units - other.minor_units, self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.minor_units, self.currency)

    def multiply_by_int(self, factor: int) -> "Money":
        """Exact scaling by a whole number. No rounding is possible."""
        if isinstance(factor, bool) or not isinstance(factor, int):
            raise MoneyPrecisionError("multiply_by_int() requires an int factor")
        return Money(self.minor_units * factor, self.currency)

    def multiply_by_ratio(self, ratio: Decimal) -> "Money":
        """Scale by a ratio, rounding once to whole minor units.

        The only place a ratio becomes money. Rounding mode is explicit and
        applied exactly once (PROJECT_RULES 4.5).
        """
        if isinstance(ratio, bool) or isinstance(ratio, float):
            raise MoneyPrecisionError("multiply_by_ratio() requires a Decimal, not a float")
        if isinstance(ratio, int):
            ratio = Decimal(ratio)
        if not isinstance(ratio, Decimal):
            raise MoneyPrecisionError(
                f"multiply_by_ratio() requires a Decimal, got {type(ratio).__name__}"
            )
        if not ratio.is_finite():
            raise MoneyPrecisionError(f"ratio must be finite, got {ratio}")
        scaled = (Decimal(self.minor_units) * ratio).quantize(
            Decimal(1), rounding=MONEY_ROUNDING
        )
        return Money(int(scaled), self.currency)

    def divide_by_int(self, divisor: int) -> "Money":
        """Split into whole minor units, rounding once.

        Used for mean ticket size. Rounding is explicit; callers that need the
        remainder to reconcile must not use this.
        """
        if isinstance(divisor, bool) or not isinstance(divisor, int):
            raise MoneyPrecisionError("divide_by_int() requires an int divisor")
        if divisor == 0:
            raise DomainValidationError("cannot divide Money by zero")
        quotient = (Decimal(self.minor_units) / Decimal(divisor)).quantize(
            Decimal(1), rounding=MONEY_ROUNDING
        )
        return Money(int(quotient), self.currency)

    # -- comparison ---------------------------------------------------------

    def __lt__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.minor_units < other.minor_units

    def __le__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.minor_units <= other.minor_units

    def __gt__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.minor_units > other.minor_units

    def __ge__(self, other: "Money") -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.minor_units >= other.minor_units

    # -- predicates ---------------------------------------------------------

    @property
    def is_zero(self) -> bool:
        return self.minor_units == 0

    @property
    def is_positive(self) -> bool:
        return self.minor_units > 0

    @property
    def is_negative(self) -> bool:
        return self.minor_units < 0

    # -- presentation -------------------------------------------------------

    def __str__(self) -> str:
        sign = "-" if self.minor_units < 0 else ""
        units = abs(self.minor_units)
        per = self.currency.minor_units_per_unit
        return f"{sign}{self.currency.symbol}{units // per}.{units % per:02d}"

    def __repr__(self) -> str:
        return f"Money({self.minor_units}, {self.currency.value})"


def sum_money(amounts, currency: Currency = Currency.INR) -> Money:
    """Total an iterable of ``Money``, exactly.

    Returns zero in ``currency`` for an empty iterable. An empty sum is a
    genuine zero (nothing was spent), unlike an undefined rate — see
    PROJECT_RULES 1.7.
    """
    total = Money.zero(currency)
    for amount in amounts:
        if not isinstance(amount, Money):
            raise MoneyPrecisionError(f"sum_money() requires Money, got {amount!r}")
        total = total + amount
    return total
