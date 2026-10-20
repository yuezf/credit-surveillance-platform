# Stage 1: deterministic leverage and the critical rule

This stage is pure Python. It does not read or write the database, load model
credentials, call an LLM, or access AWS. The database fixture, PDF, persisted
exceptions, and agent are later stages.

## Financial contract

Gross leverage = quarter-end total debt / trailing-twelve-month EBITDA ending
on the same quarter-end. No cash netting or EBITDA adjustments are inferred.

- March 31, 2025: debt $1.5B; EBITDA $400M for April 1, 2024–March 31, 2025.
- June 30, 2025: debt $1.6B; EBITDA $330M for July 1, 2024–June 30, 2025.
- Leverage strictly above 4.5x produces `breached=True`, `severity="critical"`.
- Exactly 4.5x passes. Invalid inputs produce `breached=None` and an error code.
- Amounts must be finite `Decimal` values in matching currency and scale.
- Debt can be zero but cannot be negative; EBITDA must be positive.

`FinancialObservation` is a frozen dataclass: a typed, immutable input record.
Python type annotations do not enforce values at runtime, so the calculation
also validates them. Fact IDs are retained in the output to identify inputs.
The database's `FinancialFact` has no measurement-basis/window columns yet;
we will address that explicitly when we implement the fixture adapter.

## Read the code in this order

1. `FinancialObservation`: identity, amount, and measurement definition.
2. `_input_error`: reject inputs that would make the ratio misleading.
3. `calculate_leverage`: validate, then divide with a fixed decimal context.
4. `evaluate_leverage_rule`: apply the threshold independently of ratio rounding.
5. `tests/test_leverage.py`: examples, boundaries, and failure behavior.

The rule compares `debt > Decimal("4.5") * ebitda`. With positive EBITDA this
is equivalent to comparing the ratio, but it avoids rounding a repeating
quotient before making a decision. The ratio is still returned for display.

A missing fact is not evidence that leverage is below the threshold. This is
why an uncalculable result uses `None`, rather than `False`, for `breached`.
A later service must explicitly handle this state.

Matching borrower and period IDs is a consistency check, not authorization.
The later database adapter must verify the authenticated tenant's scope, load
canonical facts, and verify their provenance before calling these functions.

## Run the focused tests

From `rag-document-assistant`:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_leverage -v
```

## Try the case without a database

From the same directory, run:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'
import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal
from app.leverage import FinancialObservation, evaluate_leverage_rule

borrower, period = uuid.uuid4(), uuid.uuid4()
debt = FinancialObservation(
    fact_id=uuid.uuid4(), borrower_id=borrower, reporting_period_id=period,
    metric="total_debt", value=Decimal("1600000000"), currency="USD",
    scale="units", basis="point_in_time", period_end=date(2025, 6, 30),
)
ebitda = replace(
    debt, fact_id=uuid.uuid4(), metric="ebitda", value=Decimal("330000000"),
    basis="ttm", period_start=date(2024, 7, 1),
)
prior_period = uuid.uuid4()
prior_debt = replace(
    debt, fact_id=uuid.uuid4(), reporting_period_id=prior_period,
    value=Decimal("1500000000"), period_end=date(2025, 3, 31),
)
prior_ebitda = replace(
    ebitda, fact_id=uuid.uuid4(), reporting_period_id=prior_period,
    value=Decimal("400000000"), period_end=date(2025, 3, 31),
    period_start=date(2024, 4, 1),
)
for label, d, e in (("prior", prior_debt, prior_ebitda), ("current", debt, ebitda)):
    result = evaluate_leverage_rule(d, e)
    print(label, f"{result.calculation.ratio:.4f}x", result.breached, result.severity)
PY
```

Expected: prior `3.7500x False None`; current `4.8485x True critical`.
These are in-memory examples, not seeded database facts.

## Your interview-style exercise

In a scratch file, write:

```python
def leverage_change(prior: LeverageCalculation, current: LeverageCalculation) -> Decimal:
    ...
```

Requirements:

- Return the absolute change in leverage turns (`current.ratio - prior.ratio`).
- Reject uncalculable inputs, different borrowers, and non-increasing period ends.
- Reject different currencies/scales or inconsistent measurement definitions.
- Use an explicit decimal context rather than inheriting the caller's precision.
- Test approximately +1.0985 turns for the example and at least two rejection cases.

Explain why a higher ratio alone cannot establish the business cause of the
change. The later investigation agent will need document evidence for that.
