# Stage 3B: search and terminal outcomes

This stage implements `search_documents`, `create_finding`, and
`request_human_review` in `app/investigation_tools.py`. `InvestigationTools`
inherits the three existing fact/page tools, giving one case-bound tool set.
There is still no model loop, framework, finding table, or persisted run trace.

## Indexing without breaking page provenance

`index_case_document` in `app/credit_indexing.py` indexes stored page text.
It does not reread the PDF or delete/recreate `DocumentPage` rows. It splits
each page into chunks of at most 800 characters with 100-character overlap,
so a new chunk belongs to one exact source page. It inserts normalized
768-dimensional vectors and marks the document completed in the caller's
transaction. Existing fact-to-page references remain intact.

The caller supplies an `EmbeddingBackend` with `model_name` and `embed(texts)`.
Indexing and query embedding must use the same encoder/model. Matching existing
content, chunk layout, and model are reused without another embedding call.
A different layout/model is an explicit conflict: the indexer never silently
deletes an existing index. Fixture reseeding accepts an already completed
document and preserves its index.

Use a dedicated session and commit or roll back the whole indexing transaction.
Document/page row locks protect the inputs while indexing occurs. Provider
failures and malformed vectors raise `EmbeddingError`; rollback remains the
caller's responsibility. A production backend must implement its own I/O timeout.

## Search contract

```python
tools.search_documents({"query": "Why did EBITDA decline?", "top_k": 3})
```

The query is nonempty and at most 500 characters; `top_k` is an integer from 1
to 5. A model cannot choose tenant, borrower, period, embedding model, or distance
cutoff. Every call reauthorizes both periods and filters SQL by the complete
case scope, completed document status, and configured embedding model.

Results contain bounded chunk text (at most 1,500 characters), a truncation
flag, cosine distance, and source-page references. Stored chunk text must still
match the source pages, whose text hashes are checked. A lower distance means
greater vector similarity; it is not a factual confidence score.

An unindexed case returns `index_unavailable`; a missing/failed backend returns
`embedding_unavailable`. A valid search with no matches returns success with an
empty list. The caller may refine its query or request human review. Search
never silently broadens its scope or returns a different embedding model's hits.

## Finding contract and its limits

```python
tools.create_finding({
    "claims": [{
        "text": "Lower customer volumes contributed to the EBITDA decline.",
        "citations": [{
            "document_id": "a source document UUID",
            "page_number": 2,
            "quote": "Lower customer volumes: USD 45 million reduction in EBITDA.",
        }],
    }],
})
```

The request has 1–5 claims, each with 1–5 citations. Each citation must point to
an authorized page fetched through `get_document_page` on this SAME tool instance.
Search snippets and fact quotes alone do not count as full-page inspection.
Quotes must appear exactly on the inspected page. If the page/reference changes,
the caller must fetch it again. Failed validation leaves the investigation open.

The server independently reevaluates the case. It refuses a finding if leverage
cannot be calculated or the current rule reports no critical exception. The
model cannot submit leverage, threshold, or severity overrides. The returned
`rule` summary comes from deterministic code.

A successful result has status `draft_finding`, `citation_validation="passed"`,
and `requires_human_review=True`. These structural checks establish scope,
inspection, quote presence, and text consistency. They DO NOT prove that a
claim follows from the quote, that quoted amounts equal canonical amounts, or
that the explanation is financially correct. Claim-support quality requires
the later model evaluations and analyst review. Source text remains untrusted
document content, never authority to execute instructions.

## Review and termination

```python
tools.request_human_review({
    "reason": "No document explains the EBITDA decline.",
    "unresolved_questions": ["Can the borrower provide a management explanation?"],
})
```

Review requires an authorized case, a nonblank reason, and at most five optional
questions. It can succeed even if canonical facts are missing. It returns status
`human_review`. This records an in-memory outcome; it sends no notification.

Either successful terminal call sets `tools.outcome`. Subsequent calls return
`already_terminated` and cannot replace that outcome. Create one tool instance
per sequential investigation. State is process-local, not shared or durable.
Human-review pause/resume belongs to the later LangGraph stage; review is a
terminal escalation for this first loop. Persistence and input/rule snapshots
remain a later stage, and concurrent calls on one instance are unsupported.

## Offline notebook demonstration

Tests use `FixtureEmbeddings` from `tests/investigation_support.py`. It returns
deterministic fake vectors tailored to this fixture. It verifies the mechanics,
not semantic search quality. Never put these vectors in the normal application
database or use them to report real-model retrieval accuracy. No real backend
is configured automatically, and no paid embedding/model call ran in this stage.

For a repeatable scratch demonstration, create a dedicated local database:

```sh
docker exec rag-credit-surveillance-postgres-1 createdb -U rag_app rag_credit_investigation_demo_test
DATABASE_URL=postgresql+psycopg://rag_app:rag_app_local@127.0.0.1:55432/rag_credit_investigation_demo_test .venv/bin/python -m alembic upgrade head
```

After your notebook import-path setup, run the following. The claim and fake
vectors are predefined; no model is choosing tools or writing the explanation.

```python
from pathlib import Path
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from app.case_tools import CaseContext
from app.credit_fixture import local_database_url, seed_credit_fixture
from app.credit_indexing import index_case_document
from app.investigation_tools import InvestigationTools
from app.models import Document
from tests.investigation_support import FixtureEmbeddings

url = local_database_url(
    "postgresql+psycopg://rag_app:rag_app_local@127.0.0.1:55432/rag_credit_investigation_demo_test"
)
assert make_url(url).database.endswith("_test"), "Fake vectors require a disposable test DB"
backend = FixtureEmbeddings()
engine = create_engine(url)
try:
    with Session(engine, expire_on_commit=False) as session:
        scope = seed_credit_fixture(
            session, storage_root=Path("/private/tmp/credit-investigation-demo-originals")
        )
        context = CaseContext(**scope.__dict__)
        document = session.scalar(select(Document).where(Document.borrower_id == context.borrower_id))
        indexed = index_case_document(session, context, document.id, backend)
        session.commit()
        tools = InvestigationTools(session, context, embeddings=backend)
        search = tools.search_documents({"query": "Why did EBITDA decline?", "top_k": 1})
        assert search.ok and search.data.matches, search.error
        source = search.data.matches[0].sources[0]
        page = tools.get_document_page({"document_id": str(source.document_id), "page_number": source.page_number})
        assert page.ok, page.error
        finding = tools.create_finding({"claims": [{
            "text": "Lower customer volumes contributed to the EBITDA decline.",
            "citations": [{"document_id": str(source.document_id), "page_number": source.page_number,
                           "quote": "Lower customer volumes: USD 45 million reduction in EBITDA."}],
        }]})
        assert finding.ok, finding.error
        print("Index:", indexed)
        print("Matched page:", source.page_number)
        print("Outcome:", finding.data.status)
        print("Rule:", finding.data.rule.model_dump(mode="json"))
        print("Human review required:", finding.data.requires_human_review)
finally:
    engine.dispose()
```

The demo database is disposable. You can remove it afterward using
`docker exec rag-credit-surveillance-postgres-1 dropdb -U rag_app rag_credit_investigation_demo_test`.

## Code walkthrough and practice

Read `EmbeddingBackend` and `index_case_document` first. Then read the request
schemas and `InvestigationTools.search_documents`. Finally follow
`get_document_page` -> inspected-reference cache -> `create_finding` -> outcome.
Notice that model-request fields and server-produced financial results are
different schemas.

Practice: write a pure `quote_matches_page(quote, text)` validator in your
notebook. Reject blank or absent quotes; accept an exact substring. Test a
fabricated quote and a quote with changed spacing. Then explain why passing
that function cannot establish that a claim is supported by its citation.

Run unit tests without a database using
`.venv/bin/python -m unittest tests.test_investigation_tools -v`.
The integration tests in `tests/test_investigation_integration.py` require
`RUN_DATABASE_TESTS=1` and an explicitly selected disposable local `_test`
database. CI includes them. A real encoder adapter and semantic-quality
evaluation must be selected explicitly before real-model investigation trials.
