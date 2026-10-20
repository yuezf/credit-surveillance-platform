# Stage 2: synthetic facts, source pages, and authorized leverage evaluation

## What this stage connects

```text
Checked-in synthetic PDF + manifest
    -> idempotent local fixture loader
    -> PostgreSQL document, extracted pages, and eight canonical facts
    -> authorize tenant + borrower + both reporting periods
    -> check each calculation fact's page ownership, quote, and text hash
    -> map FinancialFact to FinancialObservation
    -> existing pure leverage calculation and rule
```

The facts are manually specified synthetic data, not an extraction model's
predictions. Their source quotes are checked against text extracted from the PDF.
All eight facts have a source-page reference. The evaluation service currently
loads the four debt/EBITDA inputs; revenue and interest expense are available for
the later investigation tools.

## The measurements

| Metric | Q1 2025 | Q2 2025 | Basis |
|---|---:|---:|---|
| Revenue | USD 2,400 million | USD 2,200 million | TTM |
| Total debt | USD 1,500 million | USD 1,600 million | Quarter-end snapshot |
| EBITDA | USD 400 million | USD 330 million | TTM |
| Interest expense | USD 80 million | USD 95 million | TTM |

The prior TTM window is April 1, 2024 through March 31, 2025. The current window
is July 1, 2024 through June 30, 2025. Database values use USD base units, while
the PDF displays USD millions. `unit="units"` specifies scale; `currency="USD"`
specifies currency.

The second page gives a synthetic EBITDA bridge: lower volumes reduce EBITDA
by USD 45 million, and unrecovered input costs reduce it by USD 25 million.
It also attributes the USD 100 million debt increase to working capital.
These are fictional case statements for a later agent to investigate, not
conclusions produced by this stage.

## Read the implementation in this order

1. `migrations/versions/0003_fact_measurements.py` and `FinancialFact` in
   `app/models/entities.py`: add `measurement_basis`, `measurement_start`, and
   `measurement_end`. Legacy facts keep all three NULL; no quarterly/TTM basis is
   guessed. A check constraint rejects partial or inconsistent metadata.
2. `app/credit_fixture.py`: `seed_credit_fixture` joins the existing demo seed in
   one transaction using `commit=False`. Stable IDs and exact field comparisons
   make a repeated seed a no-op. Conflicting canonical facts, pages, or entity
   metadata raise `FixtureConflictError`, rather than overwriting data.
3. `app/credit_service.py`: `evaluate_credit_case` verifies both periods belong
   to the authenticated tenant and borrower, checks chronology, then loads only
   canonical debt and EBITDA. Page evidence must belong to that borrower and
   one of the two approved periods. A comparative Q1 fact can therefore cite
   the Q2 supporting document, but not an unrelated document.
4. `fact_to_observation` maps database fields into the immutable calculation
   input. It performs no database queries or tenant authorization itself.
5. The unchanged `app/leverage.py` calculates and applies the strict >4.5x rule.

The caller must derive `tenant_id` from authentication. This is an internal
service, not a new public API endpoint, and IDs are never supplied by a model.

## Run locally

From `rag-document-assistant`, with Docker Desktop running:

```sh
docker compose up -d postgres
export CREDIT_LOCAL_DATABASE_URL='postgresql+psycopg://rag_app:rag_app_local@127.0.0.1:55432/rag_credit'
DATABASE_URL="$CREDIT_LOCAL_DATABASE_URL" .venv/bin/python -m alembic upgrade head
.venv/bin/python -m app.credit_fixture --database-url "$CREDIT_LOCAL_DATABASE_URL"
```

Expected:

```text
Q1 2025: 3.7500x - pass
Q2 2025: 4.8485x - critical
```

Run the last command a second time. It must print the same output without
adding duplicate rows or changing their timestamps. The CLI requires an
explicit loopback database URL and always uses local original-PDF storage;
it does not load model credentials or the application's `.env` configuration.
The default original-PDF directory is `rag-document-assistant/data/originals`.
Use the same `--storage-root` on repeated runs.

To inspect the result in your notebook after the existing import-path setup:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.credit_service import evaluate_credit_case
from app.demo_seed import (
    DEMO_TENANT_ID, DEMO_BORROWER_ID,
    DEMO_PRIOR_PERIOD_ID, DEMO_CURRENT_PERIOD_ID,
)

engine = create_engine(
    "postgresql+psycopg://rag_app:rag_app_local@127.0.0.1:55432/rag_credit"
)
with Session(engine) as session:
    case = evaluate_credit_case(
        session, tenant_id=DEMO_TENANT_ID, borrower_id=DEMO_BORROWER_ID,
        prior_period_id=DEMO_PRIOR_PERIOD_ID,
        current_period_id=DEMO_CURRENT_PERIOD_ID,
    )
engine.dispose()
print(case.current.calculation.ratio, case.current.severity)
```

These hardcoded IDs are for the local fixture only. An API caller must use
the server-derived tenant identity instead.

## Failure semantics and current limits

- Unauthorized or unknown case scope raises `PermissionError` before facts load.
- Missing canonical facts yield `breached=None` and `missing_fact`.
- Existing facts without explicit measurement metadata or valid source-page
  evidence raise `CreditDataError`; they do not become passing calculations.
- Invalid measurement windows reach the existing pure validator and produce
  an uncalculable result. No LLM determines financial validity.
- A failed database operation rolls back the fixture transaction. If the final
  DB commit fails after the local PDF is written, a content-addressed original
  may remain on disk and can be reused on retry. File and DB storage do not share
  an atomic transaction.
- The document remains `pending`: there are deliberately no embedding rows.
  It is not yet searchable through `/search`. A later stage will index the
  existing pages without paid calls during this stage or fabricated vectors.
  Reprocessing pages requires care: existing ingestion deletes pages, which
  clears fact source-page foreign keys even if the same page IDs are recreated.
- This stage returns an exception result in memory. It does not persist an
  exception, finding, agent run, or investigation trace.

## Tests

The unit suite discovers `tests/test_credit_fixture.py` automatically.
`tests/test_credit_fixture_integration.py` requires `RUN_DATABASE_TESTS=1` and
a disposable database whose name ends in `_test`. Do not point it at the
normal demo database. CI now creates `rag_credit_test`, migrates it, checks
schema drift, and runs the new integration tests alongside the existing ones.

Coverage includes repeated seeding, preservation of a committed conflicting
fact, alternate canonical facts, storage-failure rollback, foreign tenant and
borrower scope, document-period restrictions, modified page text, missing facts,
legacy metadata, and the 3.75x/4.8485x results.

To rebuild the PDF after intentionally changing the manifest, run
`scripts/build_credit_fixture.py` with a development Python that has ReportLab
installed. The builder uses invariant output and updates the manifest's PDF
hash. Normal fixture runs and CI need only the checked-in PDF and existing
application dependencies; ReportLab is not a runtime requirement.

## Interview-style practice

Write this in your notebook without changing the production implementation:

```python
def summarize_case(case: CreditCaseResult) -> dict:
    ...
```

Return JSON-serializable values containing prior leverage, current leverage,
change in turns, threshold, and current severity. Use decimal strings for
amounts/ratios instead of converting them to floats. Reject an uncalculable
period explicitly. Use an explicit Decimal context for subtraction and round
only the displayed values to four decimal places.

For the seeded case, assert prior `"3.7500"`, current `"4.8485"`, change
`"1.0985"`, threshold `"4.5"`, and severity `"critical"`. Then construct an
uncalculable case using `dataclasses.replace` and verify your rejection path.
This practices the boundary between typed server objects and the JSON tool
results we will expose to the investigation model in the next stage.
