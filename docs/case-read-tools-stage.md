# Stage 3A: typed fact reads and exact-page lookup

This stage adds three manually callable tools in `app/case_tools.py`. They read
existing PostgreSQL rows. There is no model call, agent loop, framework, new API
endpoint, schema migration, or embedding generation in this stage.

## Design and data flow

```text
Trusted server code -> immutable CaseContext
                                     |
Request dictionary -> Pydantic validation -> recheck case authorization
                                     |
               canonical fact or exact-page SQL query
                                     |
                  measurement/evidence validation
                                     |
               typed ToolResult -> JSON-compatible payload
```

`CaseContext` binds the tenant, borrower, prior period, and current period. The
server must obtain tenant identity from authentication and select an approved
case. Constructing a context does not itself authenticate a user. This local
example uses the known demo identity; never expose context construction as a
model-controlled tool.

Each call reuses `authorize_credit_case` from `credit_service.py`: both periods
must belong to the borrower and tenant, be quarter periods, and be in order.
`evaluate_credit_case` now uses the same helper. Authorization is not cached
just because a previous call succeeded.

## Contracts

| Tool | Request arguments | Successful response |
|---|---|---|
| `get_current_fact` | `metric` | Canonical fact for the current period |
| `get_prior_fact` | `metric` | Canonical fact for the prior period |
| `get_document_page` | `document_id`, `page_number` | Full extracted text of that exact page |

Allowed metrics are `revenue`, `total_debt`, `ebitda`, and `interest_expense`.
Extra request fields, including tenant/borrower/period overrides, are rejected.
Page numbers must be positive integers; booleans, floats, and strings such as
`"2"` are rejected. Document IDs may be UUID objects or UUID strings.

Requests are parsed dictionaries. A future loop will parse the model's JSON
arguments before calling these methods. `FactRequest.model_json_schema()` and
`PageRequest.model_json_schema()` expose argument schemas for that integration.

Fact outputs preserve the fact ID, borrower and period, metric, exact Decimal
value as a string, currency, scale, basis, measurement dates, source quote, and
page reference. A page reference contains the source document's ID, name, hash,
borrower, and period, plus page ID, number, and text hash. A prior-period fact
can cite a comparative page in the current-period supporting document.

Use `.model_dump(mode="json")` on the typed result: UUIDs and dates become
strings. `ToolResult` enforces either success with data or failure with an error.
It does not allow a contradictory success/error combination.

## Errors

| Code | Meaning |
|---|---|
| `invalid_arguments` | Missing, extra, or malformed request fields |
| `scope_unavailable` | The bound case is unauthorized, unknown, or invalid |
| `fact_not_found` | No canonical fact exists for the requested metric/period |
| `page_not_found` | The page is missing or outside the case |
| `invalid_fact` | Unsupported or missing measurement metadata or invalid amount |
| `invalid_evidence` | Missing/out-of-case source page, absent quote, empty text, or hash mismatch |
| `data_unavailable` | A database/driver read failed; internal SQL details are omitted |

Unknown and out-of-case document IDs return the same public error. The tools
never return another case's page text or storage URI.

The caller owns the SQLAlchemy session. Tools use `session.no_autoflush` to
avoid writing unrelated pending objects during a read. They never commit or
roll back. Use a clean, dedicated session for tool execution; after a database
error the caller must close/discard or roll back the failed session before reuse.
Run snapshots and cross-call consistency belong to the later run-persistence
stage; these tools read the canonical records available in the supplied session.

## What the evidence checks mean

These tools read curated canonical facts; they do not extract metrics. Matching
a source quote and text hash checks page association and stored-text integrity.
It does not independently prove that the stored amount equals the quote or that
the passage supports a causal explanation. Full page text is untrusted document
content, not instructions for the future agent to execute.

The tools also do not calculate leverage or apply a credit threshold. A stored
negative EBITDA can be returned as a financial observation; the existing
calculator decides whether it can use that denominator. Debt must remain a
nonnegative quarter-end balance. Flow metrics require an explicit TTM window.

## Notebook example

Run the existing notebook import-path setup first if your notebook is in `docs`.
The local fixture must already be seeded. Then run:

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.case_tools import CaseContext, CaseReadTools
from app.demo_seed import (
    DEMO_TENANT_ID, DEMO_BORROWER_ID,
    DEMO_PRIOR_PERIOD_ID, DEMO_CURRENT_PERIOD_ID,
)

context = CaseContext(
    tenant_id=DEMO_TENANT_ID,
    borrower_id=DEMO_BORROWER_ID,
    prior_period_id=DEMO_PRIOR_PERIOD_ID,
    current_period_id=DEMO_CURRENT_PERIOD_ID,
)
engine = create_engine(
    "postgresql+psycopg://rag_app:rag_app_local@127.0.0.1:55432/rag_credit"
)
try:
    with Session(engine) as session:
        tools = CaseReadTools(session, context)
        prior = tools.get_prior_fact({"metric": "ebitda"})
        current = tools.get_current_fact({"metric": "ebitda"})
        assert prior.ok, prior.error
        assert current.ok, current.error
        print("Prior EBITDA:", prior.data.value)
        print("Current EBITDA:", current.data.value)

        reference = current.data.source
        page = tools.get_document_page({
            "document_id": str(reference.document_id),
            "page_number": reference.page_number,
        })
        assert page.ok, page.error
        assert current.data.source_text in page.data.text
        print("Source page:", page.data.source.page_number)
        print(page.data.text[:200])  # Display preview; the tool returns the full page.

        # A future model/tool loop will send this dictionary as the tool result.
        payload = current.model_dump(mode="json")
finally:
    engine.dispose()
```

Expected EBITDA amounts: `400000000.000000` and `330000000.000000`; current
evidence is on page 2. You can also inspect `payload` in a notebook cell.

## Code walkthrough and practice

Read `CaseContext`, `FactRequest`, and `PageRequest` first. Pydantic validates
their fields; `frozen=True` prevents reassignment and `extra="forbid"` rejects
unexpected fields. Type annotations alone do not provide those runtime checks.

Next read `CaseReadTools._read_fact`: validate arguments, authorize the case,
load a canonical fact, validate measurements, locate an in-scope source page,
validate page integrity/quote, and serialize. `get_document_page` follows the
same scope rules and checks the requested page's stored text hash.

Finally read `serialize_fact`. It is a pure serializer for already-authorized,
validated records, not a tool exposed to a model. `format(fact.value, "f")`
preserves Decimal precision without converting to float.

Practice in a scratch notebook: write `practice_serialize_fact(fact, page,
document)` returning the same JSON-compatible dictionary as
`serialize_fact(...).model_dump(mode="json")`. Preserve dates, UUIDs, source
references, decimal precision, and the distinction between fact period and
source-document period. Compare your output with the production serializer.
Explain why the serializer alone does not authorize database access.

## Verification

Run contract tests without database access or model settings:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_case_tools -v
```

The PostgreSQL tool tests are in `tests/test_case_tools_integration.py`. Like
the fixture tests, they require an explicitly selected disposable local database
ending in `_test` and `RUN_DATABASE_TESTS=1`. CI includes the new module in its
integration-test step. Pydantic is constrained to version 2 because this stage
uses its runtime validation and JSON-schema APIs.
