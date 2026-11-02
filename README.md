# Credit Surveillance Platform

A tenant-scoped backend for borrower-document retrieval and evidence-backed credit investigation. The system preserves PDF provenance, calculates leverage from canonical financial facts, and uses a bounded analyst agent to investigate changes with page-level citations.

**Python · FastAPI · PostgreSQL / pgvector · SQLAlchemy · Gemini / OpenAI-compatible APIs · Docker**

The document API and local analyst agent are implemented. The agent currently covers one borrower, two reporting periods, and one leverage rule. Findings remain drafts requiring analyst review; durable run storage and approval/resume are the next development stages.

[Architecture](#architecture) · [Local setup](#local-setup) · [API](#api) · [Credit investigation](#credit-investigation) · [Testing](#testing) · [Documentation](#documentation)

## Capabilities

| Component | Behavior |
| --- | --- |
| Document ingestion | Stores original PDF bytes, extracted pages, chunks, and vector embeddings; identifies duplicate uploads using tenant-scoped content hashes. |
| Retrieval and answers | Searches PostgreSQL/pgvector within the authenticated tenant, with optional document, borrower, and period filters. Answers include retrieved source references. |
| Financial calculations | Uses `Decimal` arithmetic and explicit measurement windows to calculate gross debt / TTM EBITDA and evaluate a strict leverage threshold. |
| Analyst agent | Chooses among six case-scoped tools, reads facts and evidence, and returns a cited draft finding or a human-review escalation. |
| Evidence validation | Checks citation scope, prior full-page inspection, page hashes, and exact quote presence before accepting an agent finding. |
| Execution controls | Limits model turns, attempted tool calls, and wall-clock time; returns explicit termination reasons and an inspectable in-memory trace. |

## Architecture

```mermaid
flowchart TB
    Client[API client] --> API[FastAPI + tenant API-key authentication]
    API --> Ingest[PDF extraction, chunking, and embeddings]
    Ingest --> Originals[Original PDF storage: local or S3]
    Ingest --> DB[(PostgreSQL + pgvector)]
    API --> Retrieval[Tenant-scoped retrieval]
    Retrieval --> DB
    Retrieval --> Answer[LLM answer with source references]

    Fixture[Curated financial fixture] --> DB
    Case[Trusted tenant, borrower, and period context] --> Rules[Deterministic leverage evaluation]
    Rules --> DB
    Rules --> Agent[Bounded model/tool loop]
    Agent --> Tools[Case-scoped fact, search, page, and outcome tools]
    Tools --> DB
    Tools --> Outcome[Draft finding or human-review escalation]
```

The HTTP API provides document ingestion, search, and answers. Credit investigation is a separate Python service interface; there is no agent HTTP endpoint yet.

### Responsibility boundaries

- **Deterministic services** own canonical facts, measurement definitions, calculations, threshold decisions, authorization, and structural citation validation.
- **The model** chooses investigative actions, interprets evidence, and proposes an explanation or requests human review. It cannot override the financial rule or choose a different tenant or case.
- **The analyst** reviews the draft. Quote validation establishes provenance and text presence; it does not independently prove that a claim follows from the evidence.

API identity comes from an active, hash-stored service key. Agent tools receive a trusted `CaseContext` from server-side code, and recheck the borrower/period relationships. A run ID or model-supplied identifier is not an authorization credential.

### Storage and indexing

PostgreSQL stores tenants, borrowers, reporting periods, document metadata, page text, chunks, canonical facts, and 768-dimensional vectors. Original PDF bytes are stored separately under content-addressed local paths or S3 keys.

Document IDs derive from tenant identity and the original file hash. Duplicate uploads reuse existing documents unless reprocessing is explicitly requested. The RAG ingestion path supports chunks spanning page boundaries. The credit indexer chunks stored pages individually and preserves page IDs and fact-to-page references; a conflicting index/model is rejected rather than silently replaced.

## Local setup

### Requirements

- Python 3.12 for the development/CI workflow.
- Docker with Compose for PostgreSQL 16 and pgvector.
- A configured chat model and embedding model accessible through an OpenAI-compatible endpoint. Agent use requires function calling; embeddings must support the `dimensions` parameter with 768-dimensional output.
- macOS or Linux for the current main-thread agent deadline implementation. The document API does not depend on that timer.

From the repository root:

```bash
cd rag-document-assistant
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Set the provider settings in `.env`. The example contains placeholders, not a working model configuration.

| Variable | Purpose |
| --- | --- |
| `MODEL_BASE_URL` | OpenAI-compatible provider endpoint. |
| `MODEL_API_KEY` | Provider credential; required by the current configuration loader. |
| `LLM_MODEL` | Chat model used for answers and agent tool selection. |
| `EMBEDDING_MODEL` | Encoder used for document chunks and search queries. |
| `EMBEDDING_DIMENSION` | Must be `768` for the current database schema. |
| `DATABASE_URL` | SQLAlchemy PostgreSQL URL; local defaults are supplied in `.env.example`. |
| `ORIGINAL_PDF_STORAGE_BACKEND` | `local` by default; `s3` is also supported. |
| `ORIGINAL_PDF_LOCAL_ROOT` | Optional persistent directory; defaults to the application's `data/originals/`. |
| `ORIGINAL_PDF_S3_BUCKET` | Required when S3 storage is selected. AWS credentials use the SDK credential chain. |

Start the local database, apply migrations, and create a sample tenant/borrower scope:

```bash
docker compose up -d postgres
docker compose ps
python -m alembic upgrade head
python -m app.demo_seed
```

The database is exposed on host port **55432**. The default local connection is:

```text
postgresql+psycopg://rag_app:rag_app_local@localhost:55432/rag_credit
```

The seed command prints tenant, borrower, and reporting-period IDs. Use the tenant ID to issue a service key, replacing the placeholder below:

```bash
python -m app.create_api_key --tenant-id <tenant-id> --label local-client
```

The key is displayed once; PostgreSQL stores its hash. Keep it outside source control and provide it through the `X-API-Key` request header. Use HTTPS when sending keys over a network. The current identity model provides tenant-level service authentication, not individual analyst accounts or roles.

Start the API from `rag-document-assistant/`:

```bash
python -m uvicorn app.api:app --reload
```

The service listens at `http://127.0.0.1:8000`. Interactive request/response schemas are available at [`/docs`](http://127.0.0.1:8000/docs).

## API

| Method | Endpoint | Authentication | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health` | Public | Process liveness. |
| `GET` | `/health/ready` | Public | PostgreSQL connectivity; returns `503` when unavailable. |
| `POST` | `/ingest` | `X-API-Key` | Upload PDFs for an authorized borrower and reporting period. |
| `POST` | `/search` | `X-API-Key` | Retrieve tenant-scoped document chunks and provenance. |
| `POST` | `/answer` | `X-API-Key` | Generate an answer from retrieved context and return sources. |
| `GET` | `/collection-info` | `X-API-Key` | Tenant-scoped document, page, and chunk counts. |

The examples assume `CREDIT_API_KEY`, `BORROWER_ID`, and `REPORTING_PERIOD_ID` are set in your shell using the issued key and seeded IDs. Run them from `rag-document-assistant/`.

### Ingest a document

Replace the PDF path with a borrower report on your machine.

```bash
curl --fail-with-body http://127.0.0.1:8000/ingest \
  -H "X-API-Key: $CREDIT_API_KEY" \
  -F "files=@/absolute/path/to/borrower-report.pdf" \
  -F "borrower_id=$BORROWER_ID" \
  -F "reporting_period_id=$REPORTING_PERIOD_ID"
```

Optional fields are `chunk_size` (default `500` characters), `overlap` (default `50`), and `force_reprocess` (default `false`). Ingestion is synchronous. Uploading a PDF indexes text; it does not extract canonical financial facts.

### Search or answer

```bash
curl --fail-with-body http://127.0.0.1:8000/search \
  -H "X-API-Key: $CREDIT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"queries":["What explains the EBITDA decline?"],"top_k":3}'

curl --fail-with-body http://127.0.0.1:8000/answer \
  -H "X-API-Key: $CREDIT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What explains the EBITDA decline?","top_k":3}'
```

Both requests accept optional `document_id`, `borrower_id`, and `reporting_period_id` filters. These narrow the authenticated tenant's scope. `max_distance` defaults to `0.6`; it is a vector-distance cutoff, not a calibrated confidence score. The `/answer` route is a retrieval-and-generation request, distinct from the iterative analyst agent.

## Credit investigation

The reference case uses Example Corp, Q1/Q2 2025, and four metrics: revenue, total debt, EBITDA, and interest expense. Financial facts are curated in JSON and seeded into PostgreSQL alongside extracted synthetic PDF text. They are not produced by an LLM extraction pipeline.

| Measurement | Q1 2025 | Q2 2025 |
| --- | --- | --- |
| Gross debt at quarter-end | $1.5B | $1.6B |
| Trailing-twelve-month EBITDA | $400M | $330M |
| Gross debt / TTM EBITDA | 3.75× | ≈4.85× |
| Strict rule: leverage > 4.5× | Pass | Critical |

From `rag-document-assistant/`, seed and evaluate the case against the local database:

```bash
python -m app.credit_fixture \
  --database-url postgresql+psycopg://rag_app:rag_app_local@127.0.0.1:55432/rag_credit
```

The seed is idempotent: it inserts missing records, verifies existing fixture values, and preserves a matching completed index. It uses local PDF storage and does not generate embeddings. For a separate investigation exercise, use a fresh local database as described in the [indexing walkthrough](docs/search-outcomes-stage.md); avoid reprocessing fixture pages through the generic ingestion path after facts have been linked to them.

### Agent interface

`run_investigation()` takes a database session, trusted case context, model adapter, and embedding backend. The case document must be indexed with the same embedding model used for search. The model chooses among:

| Tool | Responsibility |
| --- | --- |
| `get_current_fact` / `get_prior_fact` | Read a canonical metric with its measurement definition and provenance. |
| `search_documents` | Retrieve evidence within the fixed borrower/period scope. |
| `get_document_page` | Inspect the full page before citing it. |
| `create_finding` | Validate citations and return a draft with server-calculated leverage and severity. |
| `request_human_review` | Return an escalation reason and unresolved questions. |

The default budgets are 12 model turns, 24 attempted tool calls, and 120 seconds per run. Calls execute sequentially. A valid draft or escalation ends the run immediately; limits, timeouts, invalid model responses, and fatal errors produce explicit termination reasons.

The result contains tool arguments/results, model-reported usage, elapsed time, and any terminal outcome. It lives in memory and can be exported as JSON; it is not yet persisted in application tables. Human review is an outcome, not a notification, approval queue, or resumable workflow.

See the [notebook setup and execution guide](docs/bounded-agent-stage.md) for a complete invocation. Model and embedding requests use the configured provider and are subject to that project's billing tier and quotas.

## Testing

GitHub Actions applies migrations, checks model/schema drift, runs unit tests, and executes six PostgreSQL integration-test modules against a fresh pgvector database. External model calls are mocked in automated tests.

Run unit tests from `rag-document-assistant/`:

```bash
python -m unittest discover -s tests -v
```

For the complete suite, use a **fresh, disposable local database ending in `_test`**. The agent integration suite requires an empty database and owns its fixture data. Do not point it at the application database or an existing investigation database.

```bash
docker exec rag-credit-surveillance-postgres-1 createdb -U rag_app rag_credit_suite_test
TEST_DATABASE_URL=postgresql+psycopg://rag_app:rag_app_local@127.0.0.1:55432/rag_credit_suite_test
DATABASE_URL="$TEST_DATABASE_URL" python -m alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" python -m alembic check
RUN_DATABASE_TESTS=1 DATABASE_URL="$TEST_DATABASE_URL" python -m unittest discover -s tests -v
```

Coverage includes tenant isolation, original-byte storage, duplicate ingestion, transaction rollback, provenance validation, financial measurement semantics, exact threshold behavior, tool contracts, fabricated citations, missing evidence, unauthorized scope, and loop limits/timeouts.

Recorded validation: **160 passing tests**, including 62 PostgreSQL tests. A real embedding smoke test retrieved the expected supporting page; a live agent investigation produced a page-cited draft in three model turns and eight tool calls. These are single-case checks, not broad accuracy, cost, or latency benchmarks. See the [embedding results](docs/embedding-run-2026-10-25.md) and [agent trace/report](docs/agent-run-2026-10-26.md).

## Deployment and operating limits

The repository includes an API Dockerfile and local database Compose service. The RAG API has been deployed and verified on ECS Fargate with private RDS PostgreSQL/pgvector, S3 original storage, IAM roles, Secrets Manager, and HTTPS. The deployment record verifies ingestion, search, answers, original-byte integrity, and retrieval after application-task replacement. It does not establish current availability, backup restoration, or production readiness. The analyst agent is developed and tested locally.

Operational boundaries to account for:

- **Extraction:** text PDFs only; OCR, reliable table reconstruction, and automated financial-fact extraction are not implemented.
- **Execution:** ingestion is synchronous. The agent's POSIX deadline requires a macOS/Linux main thread; it is not a threaded web-server or background-worker runtime.
- **Durability:** agent steps and outcomes lack database persistence, restart recovery, and approval/resume. Model-call retries are disabled in the loop adapter.
- **Evidence quality:** exact quote validation does not verify claim entailment. Retrieval thresholds and explanation quality need a broader evaluation set.
- **Operations:** individual analyst roles, request rate limits, retention workflows, production monitoring, and backup/restore validation remain outstanding.

Next work focuses on durable input/rule snapshots and run traces, broader agent evaluations, and resumable analyst review. Background execution and retry semantics follow that foundation.

## Repository layout

```text
rag-document-assistant/
├── app/
│   ├── api.py, auth.py                   # HTTP interface and tenant identity
│   ├── document_service.py              # Transactional document persistence
│   ├── document_repository.py           # Scoped SQL and vector retrieval
│   ├── ingestion.py, chunking.py        # Page extraction and chunk provenance
│   ├── original_storage.py              # Local/S3 original bytes
│   ├── leverage.py, credit_service.py   # Deterministic financial evaluation
│   ├── case_tools.py                    # Typed fact/page contracts
│   ├── credit_indexing.py               # Page-preserving credit index
│   ├── investigation_tools.py           # Search, findings, and escalation
│   ├── agent_loop.py, agent_model.py    # Bounded execution and model adapter
│   ├── embedding_backend.py             # Explicit embedding-service adapter
│   └── models/                          # SQLAlchemy entities
├── migrations/                          # Alembic schema versions
├── tests/                               # Unit and PostgreSQL integration tests
├── eval/                                # Retrieval evaluation scaffold
├── compose.yaml                         # Local PostgreSQL/pgvector
└── Dockerfile                           # API container
```

## Documentation

- [Credit calculations and measurement semantics](docs/leverage-stage.md)
- [Canonical fixture and case evaluation](docs/credit-fixture-stage.md)
- [Fact/page tool contracts](docs/case-read-tools-stage.md)
- [Credit indexing, retrieval, and outcome contracts](docs/search-outcomes-stage.md)
- [Embedding adapter](docs/embedding-adapter-stage.md)
- [Agent loop, notebook execution, and failure modes](docs/bounded-agent-stage.md)
- [Development roadmap](docs/agent-learning-roadmap.md)
- [AWS architecture and deployment reference](docs/aws-rag-lab/README.md)
- [AWS verification record](docs/aws-rag-lab/verification.md)
- [AWS cost and teardown reference](docs/aws-rag-lab/cost-and-teardown.md)

This repository continues the history of [Chat-with-PDF-RAG-System](https://github.com/yuezf/Chat-with-PDF-RAG-System), the original document-RAG implementation.
