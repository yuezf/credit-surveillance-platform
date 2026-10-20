"""Pure credit calculations: no database, credentials, or model calls.

Callers must authorize and load canonical facts before constructing observations.
Matching borrower/period IDs here checks consistency, not tenant authorization.
"""

import calendar
import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Context, Decimal, localcontext
from typing import Literal


LEVERAGE_THRESHOLD = Decimal("4.5")


@dataclass(frozen=True)
class FinancialObservation:
    fact_id: uuid.UUID
    borrower_id: uuid.UUID
    reporting_period_id: uuid.UUID
    metric: Literal["total_debt", "ebitda"]
    value: Decimal
    currency: str
    scale: Literal["units", "millions"]
    basis: Literal["point_in_time", "ttm"]
    period_end: date
    period_start: date | None = None


@dataclass(frozen=True)
class LeverageCalculation:
    debt: FinancialObservation | None
    ebitda: FinancialObservation | None
    ratio: Decimal | None
    error_code: str | None = None

    @property
    def status(self) -> Literal["calculated", "uncalculable"]:
        return "calculated" if self.error_code is None else "uncalculable"


@dataclass(frozen=True)
class LeverageRuleResult:
    calculation: LeverageCalculation
    threshold: Decimal
    breached: bool | None
    severity: Literal["critical"] | None


def _ttm_start(period_end: date) -> date:
    """Inclusive twelve-month window, including leap-year quarter ends."""
    previous_year = period_end.year - 1
    previous_day = min(
        period_end.day, calendar.monthrange(previous_year, period_end.month)[1]
    )
    return period_end.replace(year=previous_year, day=previous_day) + timedelta(days=1)


def _input_error(
    debt: FinancialObservation | None, ebitda: FinancialObservation | None
) -> str | None:
    if debt is None or ebitda is None:
        return "missing_fact"
    if debt.metric != "total_debt" or ebitda.metric != "ebitda":
        return "wrong_metric"
    if any(
        not isinstance(value, uuid.UUID)
        for observation in (debt, ebitda)
        for value in (
            observation.fact_id, observation.borrower_id, observation.reporting_period_id
        )
    ):
        return "invalid_identity"
    if (
        debt.borrower_id != ebitda.borrower_id
        or debt.reporting_period_id != ebitda.reporting_period_id
    ):
        return "scope_mismatch"
    if any(
        not isinstance(observation.value, Decimal) or not observation.value.is_finite()
        for observation in (debt, ebitda)
    ):
        return "invalid_amount"
    if debt.value < 0:
        return "negative_debt"
    if ebitda.value <= 0:
        return "nonpositive_ebitda"
    if any(
        not isinstance(observation.currency, str)
        or re.fullmatch(r"[A-Z]{3}", observation.currency) is None
        for observation in (debt, ebitda)
    ):
        return "invalid_currency"
    if debt.currency != ebitda.currency:
        return "currency_mismatch"
    if debt.scale not in ("units", "millions") or ebitda.scale not in ("units", "millions"):
        return "invalid_scale"
    if debt.scale != ebitda.scale:
        return "scale_mismatch"
    if debt.basis != "point_in_time" or ebitda.basis != "ttm":
        return "basis_mismatch"
    if any(type(observation.period_end) is not date for observation in (debt, ebitda)):
        return "invalid_period"
    if debt.period_end != ebitda.period_end:
        return "period_end_mismatch"
    end = debt.period_end
    if end.year <= 1 or end.month not in (3, 6, 9, 12) or end.day != calendar.monthrange(end.year, end.month)[1]:
        return "not_quarter_end"
    if debt.period_start is not None or ebitda.period_start != _ttm_start(end):
        return "invalid_measurement_window"
    return None


def calculate_leverage(
    debt: FinancialObservation | None, ebitda: FinancialObservation | None
) -> LeverageCalculation:
    """Return gross debt / TTM EBITDA, or a machine-readable invalid-input result.

    Currency and scale must match; this function performs no FX or unit conversion.
    Ratios use a fixed 34-digit precision independent of the caller's context.
    """
    error = _input_error(debt, ebitda)
    if error is not None:
        return LeverageCalculation(debt, ebitda, ratio=None, error_code=error)
    assert debt is not None and ebitda is not None
    with localcontext(Context(prec=34)):
        ratio = debt.value / ebitda.value
    return LeverageCalculation(debt, ebitda, ratio=ratio)


def evaluate_leverage_rule(
    debt: FinancialObservation | None, ebitda: FinancialObservation | None
) -> LeverageRuleResult:
    """Critical when gross leverage is strictly ABOVE 4.5x; equality passes.

    Compare amounts directly so rounding a repeating quotient cannot change the
    decision. Invalid inputs have breached=None, never a misleading False.
    """
    calculation = calculate_leverage(debt, ebitda)
    if calculation.status == "uncalculable":
        return LeverageRuleResult(calculation, LEVERAGE_THRESHOLD, None, None)
    assert debt is not None and ebitda is not None
    precision = max(
        34,
        len(ebitda.value.as_tuple().digits)
        + len(LEVERAGE_THRESHOLD.as_tuple().digits),
    )
    with localcontext(Context(prec=precision)):
        breached = debt.value > LEVERAGE_THRESHOLD * ebitda.value
    return LeverageRuleResult(
        calculation, LEVERAGE_THRESHOLD, breached, "critical" if breached else None
    )
