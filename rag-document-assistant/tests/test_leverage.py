import unittest
import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext

from app.leverage import FinancialObservation, calculate_leverage, evaluate_leverage_rule


class LeverageTests(unittest.TestCase):
    def setUp(self):
        borrower = uuid.uuid4()
        period = uuid.uuid4()
        self.debt = FinancialObservation(
            fact_id=uuid.uuid4(), borrower_id=borrower, reporting_period_id=period,
            metric="total_debt", value=Decimal("1600000000"), currency="USD",
            scale="units", basis="point_in_time", period_end=date(2025, 6, 30),
        )
        self.ebitda = FinancialObservation(
            fact_id=uuid.uuid4(), borrower_id=borrower, reporting_period_id=period,
            metric="ebitda", value=Decimal("330000000"), currency="USD",
            scale="units", basis="ttm", period_end=date(2025, 6, 30),
            period_start=date(2024, 7, 1),
        )

    def assert_uncalculable(self, debt, ebitda, code):
        result = evaluate_leverage_rule(debt, ebitda)
        self.assertEqual(result.calculation.status, "uncalculable")
        self.assertEqual(result.calculation.error_code, code)
        self.assertIsNone(result.calculation.ratio)
        self.assertIsNone(result.breached)
        self.assertIsNone(result.severity)

    def test_prior_period_is_375_and_passes(self):
        debt = replace(self.debt, value=Decimal("1500000000"), period_end=date(2025, 3, 31))
        ebitda = replace(self.ebitda, value=Decimal("400000000"),
                         period_end=date(2025, 3, 31), period_start=date(2024, 4, 1))
        result = evaluate_leverage_rule(debt, ebitda)
        self.assertEqual(result.calculation.ratio, Decimal("3.75"))
        self.assertFalse(result.breached)
        self.assertIsNone(result.severity)

    def test_current_period_is_critical_and_keeps_fact_references(self):
        result = evaluate_leverage_rule(self.debt, self.ebitda)
        self.assertEqual(result.calculation.ratio.quantize(Decimal("0.0001")), Decimal("4.8485"))
        self.assertTrue(result.breached)
        self.assertEqual(result.severity, "critical")
        self.assertEqual(result.threshold, Decimal("4.5"))
        self.assertEqual(result.calculation.debt.fact_id, self.debt.fact_id)
        self.assertEqual(result.calculation.ebitda.fact_id, self.ebitda.fact_id)

    def test_threshold_boundary_and_display_rounding(self):
        ebitda = replace(self.ebitda, value=Decimal("100"))
        for value, expected in (("449.999999", False), ("450", False), ("450.000001", True)):
            with self.subTest(value=value):
                result = evaluate_leverage_rule(replace(self.debt, value=Decimal(value)), ebitda)
                self.assertEqual(result.breached, expected)
                self.assertEqual(result.calculation.ratio.quantize(Decimal("0.01")), Decimal("4.50"))

    def test_rule_does_not_depend_on_rounded_quotient(self):
        # More precision than the displayed/calculated ratio: exact comparison still breaches.
        ebitda = replace(self.ebitda, value=Decimal("1"))
        debt = replace(self.debt, value=Decimal("4.50000000000000000000000000000000001"))
        result = evaluate_leverage_rule(debt, ebitda)
        self.assertEqual(result.calculation.ratio, Decimal("4.5"))
        self.assertTrue(result.breached)

    def test_caller_decimal_context_does_not_change_calculation(self):
        expected = evaluate_leverage_rule(self.debt, self.ebitda)
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_DOWN
            context.traps[Inexact] = True
            actual = evaluate_leverage_rule(self.debt, self.ebitda)
        self.assertEqual(actual, expected)

    def test_missing_facts_are_not_a_pass(self):
        for debt, ebitda in ((None, self.ebitda), (self.debt, None), (None, None)):
            with self.subTest(debt=debt, ebitda=ebitda):
                self.assert_uncalculable(debt, ebitda, "missing_fact")

    def test_rejects_float_and_nonfinite_amounts(self):
        for value in (1.6e9, Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity")):
            for target in ("debt", "ebitda"):
                with self.subTest(value=value, target=target):
                    debt = replace(self.debt, value=value) if target == "debt" else self.debt
                    ebitda = replace(self.ebitda, value=value) if target == "ebitda" else self.ebitda
                    self.assert_uncalculable(debt, ebitda, "invalid_amount")

    def test_nonpositive_ebitda_and_negative_debt(self):
        for value in (Decimal("0"), Decimal("-1")):
            self.assert_uncalculable(self.debt, replace(self.ebitda, value=value), "nonpositive_ebitda")
        self.assert_uncalculable(replace(self.debt, value=Decimal("-1")), self.ebitda, "negative_debt")

    def test_zero_debt_is_valid(self):
        result = evaluate_leverage_rule(replace(self.debt, value=Decimal("0")), self.ebitda)
        self.assertEqual(result.calculation.ratio, Decimal("0"))
        self.assertFalse(result.breached)

    def test_requires_matching_borrower_and_reporting_period(self):
        for field in ("borrower_id", "reporting_period_id"):
            with self.subTest(field=field):
                self.assert_uncalculable(self.debt, replace(self.ebitda, **{field: uuid.uuid4()}), "scope_mismatch")

    def test_rejects_incompatible_currency_and_scale(self):
        for changes, code in (({"currency": "EUR"}, "currency_mismatch"),
                              ({"currency": "usd"}, "invalid_currency"),
                              ({"scale": "millions"}, "scale_mismatch"),
                              ({"scale": "thousands"}, "invalid_scale")):
            with self.subTest(changes=changes):
                self.assert_uncalculable(self.debt, replace(self.ebitda, **changes), code)

    def test_matching_millions_have_same_ratio(self):
        result = calculate_leverage(
            replace(self.debt, value=Decimal("1600"), scale="millions"),
            replace(self.ebitda, value=Decimal("330"), scale="millions"),
        )
        self.assertEqual(result.ratio, calculate_leverage(self.debt, self.ebitda).ratio)

    def test_requires_debt_snapshot_and_ttm_ebitda(self):
        self.assert_uncalculable(replace(self.debt, basis="ttm"), self.ebitda, "basis_mismatch")
        self.assert_uncalculable(self.debt, replace(self.ebitda, basis="point_in_time"), "basis_mismatch")

    def test_requires_aligned_quarter_end_and_exact_ttm_window(self):
        self.assert_uncalculable(self.debt, replace(self.ebitda, period_end=date(2025, 3, 31)), "period_end_mismatch")
        self.assert_uncalculable(self.debt, replace(self.ebitda, period_start=date(2025, 4, 1)), "invalid_measurement_window")
        self.assert_uncalculable(replace(self.debt, period_start=date(2025, 4, 1)), self.ebitda, "invalid_measurement_window")
        self.assert_uncalculable(replace(self.debt, period_end=date(2025, 6, 29)),
                                replace(self.ebitda, period_end=date(2025, 6, 29)), "not_quarter_end")

    def test_leap_year_ttm_window(self):
        result = calculate_leverage(
            replace(self.debt, period_end=date(2024, 3, 31)),
            replace(self.ebitda, period_end=date(2024, 3, 31), period_start=date(2023, 4, 1)),
        )
        self.assertEqual(result.status, "calculated")

    def test_wrong_metrics_and_invalid_identity_or_date(self):
        self.assert_uncalculable(replace(self.debt, metric="ebitda"), self.ebitda, "wrong_metric")
        self.assert_uncalculable(self.debt, replace(self.ebitda, fact_id="unvalidated"), "invalid_identity")
        self.assert_uncalculable(self.debt, replace(self.ebitda, period_end="2025-06-30"), "invalid_period")


if __name__ == "__main__":
    unittest.main()
