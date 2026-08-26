"""Tests for ``domain.money`` — the most safety-critical contract in the system.

Every rupee figure the merchant sees passes through this type. The tests that
matter most here are the *refusals*: a ``Money`` that quietly accepts a float, or
rounds a sub-paise value instead of rejecting it, produces figures that are
plausible and wrong.
"""

import unittest
from decimal import Decimal

from ...domain.enums import Currency
from ...domain.errors import DomainValidationError, MoneyPrecisionError
from ...domain.money import Money, sum_money


class MoneyConstructionTests(unittest.TestCase):
    def test_paise_are_stored_exactly(self):
        self.assertEqual(Money(12_345).minor_units, 12_345)
        self.assertEqual(Money.from_paise(1).minor_units, 1)
        self.assertEqual(Money.zero().minor_units, 0)

    def test_from_rupees_accepts_int_str_and_decimal(self):
        self.assertEqual(Money.from_rupees(100).minor_units, 10_000)
        self.assertEqual(Money.from_rupees("100.50").minor_units, 10_050)
        self.assertEqual(Money.from_rupees(Decimal("0.01")).minor_units, 1)

    def test_float_is_rejected(self):
        # 0.1 + 0.2 != 0.3 in binary floating point. Accepting a float here would
        # let that error into a revenue figure.
        with self.assertRaises(MoneyPrecisionError):
            Money(100.0)
        with self.assertRaises(MoneyPrecisionError):
            Money.from_rupees(100.5)

    def test_bool_is_rejected_before_int(self):
        # bool is a subclass of int, so True would silently become 1 paisa.
        with self.assertRaises(MoneyPrecisionError):
            Money(True)
        with self.assertRaises(MoneyPrecisionError):
            Money(False)

    def test_sub_paise_precision_is_rejected_not_rounded(self):
        # Rounding here would be a silent loss. The caller must decide.
        with self.assertRaises(MoneyPrecisionError):
            Money.from_rupees(Decimal("1.005"))
        with self.assertRaises(MoneyPrecisionError):
            Money.from_rupees("0.001")

    def test_negative_amounts_are_representable(self):
        # Money itself allows negatives; contracts that must not be negative
        # (Payment.amount, RevenueRisk) enforce that themselves.
        self.assertEqual(Money(-500).minor_units, -500)
        self.assertTrue(Money(-500).is_negative)


class MoneyArithmeticTests(unittest.TestCase):
    def test_addition_and_subtraction_are_exact(self):
        self.assertEqual(Money(10_050) + Money(2_575), Money(12_625))
        self.assertEqual(Money(10_050) - Money(2_575), Money(7_475))
        self.assertEqual(-Money(500), Money(-500))

    def test_multiply_by_int_is_exact(self):
        self.assertEqual(Money(333).multiply_by_int(3), Money(999))

    def test_multiply_by_ratio_rounds_half_up_once(self):
        # 100.00 x 0.005 = 0.50 exactly.
        self.assertEqual(Money(10_000).multiply_by_ratio(Decimal("0.005")), Money(50))
        # 1 paisa x 0.5 = 0.5 paise -> 1 paisa (half up, away from zero).
        self.assertEqual(Money(1).multiply_by_ratio(Decimal("0.5")), Money(1))
        # 3 paise x 0.5 = 1.5 paise -> 2 paise.
        self.assertEqual(Money(3).multiply_by_ratio(Decimal("0.5")), Money(2))
        # 5 paise x 0.5 = 2.5 paise -> 3 paise. Banker's rounding would give 2.
        self.assertEqual(Money(5).multiply_by_ratio(Decimal("0.5")), Money(3))

    def test_multiply_by_ratio_rejects_float(self):
        with self.assertRaises(MoneyPrecisionError):
            Money(10_000).multiply_by_ratio(0.005)

    def test_divide_by_int_rounds_half_up(self):
        # 10 paise / 3 = 3.333... -> 3
        self.assertEqual(Money(10).divide_by_int(3), Money(3))
        # 100.00 / 3 = 33.3333 -> 33.33
        self.assertEqual(Money(10_000).divide_by_int(3), Money(3_333))

    def test_divide_by_zero_raises(self):
        with self.assertRaises(DomainValidationError):
            Money(10_000).divide_by_int(0)

    def test_large_amounts_do_not_lose_precision(self):
        # 10 crore rupees in paise. A float64 mantissa holds 53 bits (~9e15), so
        # this is fine for float too, but the point is exactness at every step.
        crore = Money.from_rupees(100_000_000)
        self.assertEqual(crore.minor_units, 10_000_000_000)
        self.assertEqual((crore + Money(1)).minor_units, 10_000_000_001)
        # A ratio applied to a large amount stays exact to the paisa.
        self.assertEqual(
            crore.multiply_by_ratio(Decimal("0.0001")).minor_units, 1_000_000
        )


class CurrencyGuardTests(unittest.TestCase):
    """There is one currency today. The guards exist so adding a second is safe.

    Known coverage gap: ``Currency`` has a single member, so a genuine mismatch
    cannot be constructed and ``CurrencyMismatchError`` is unexercised. Adding a
    second currency must come with tests that a cross-currency add, subtract, and
    compare all raise. Stated here rather than left implicit, because an
    unexercised guard is not a verified guard (ARCHITECTURE.md 22, Q5).
    """

    def test_same_currency_operations_succeed(self):
        a = Money(100, Currency.INR)
        b = Money(200, Currency.INR)
        self.assertEqual((a + b).currency, Currency.INR)

    def test_sum_money_of_empty_is_zero_in_stated_currency(self):
        # Distinct from a rate: summing no money genuinely IS zero, whereas a
        # rate over nobody is undefined. Different questions, different answers.
        total = sum_money([], Currency.INR)
        self.assertEqual(total, Money.zero(Currency.INR))

    def test_sum_money_adds_all_amounts(self):
        self.assertEqual(
            sum_money([Money(100), Money(250), Money(3)]), Money(353)
        )


class MoneyComparisonTests(unittest.TestCase):
    def test_ordering(self):
        self.assertLess(Money(100), Money(200))
        self.assertGreater(Money(200), Money(100))
        self.assertLessEqual(Money(100), Money(100))
        self.assertEqual(Money(100), Money(100))

    def test_equality_is_value_based(self):
        self.assertEqual(Money(100, Currency.INR), Money(100, Currency.INR))
        self.assertEqual(hash(Money(100)), hash(Money(100)))

    def test_predicates(self):
        self.assertTrue(Money.zero().is_zero)
        self.assertTrue(Money(1).is_positive)
        self.assertTrue(Money(-1).is_negative)
        self.assertFalse(Money.zero().is_positive)

    def test_str_renders_rupees(self):
        self.assertEqual(str(Money(12_34)), "₹12.34")
        self.assertEqual(str(Money(1)), "₹0.01")


class MoneyImmutabilityTests(unittest.TestCase):
    def test_money_cannot_be_mutated(self):
        amount = Money(100)
        with self.assertRaises(Exception):
            amount.minor_units = 999  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
