# Stage 4: a bounded model/tool loop

This stage connects the six existing tools to a real tool-calling model. It adds
no framework, API route, schema migration, background worker, or database run
persistence. A run returns an in-memory result and steps that can be displayed or
exported. Review is a terminal escalation, not a resumable approval workflow yet.

## Architecture and code walkthrough

Read `rag-document-assistant/app/agent_loop.py` in this order:

1. `SYSTEM_PROMPT` defines the investigation and evidence policy. It does not
   contain the explanation or tell the model a fixed tool sequence. Document
   text is untrusted data. Prompt instructions alone are not an authorization or
   citation-security boundary; existing server-side tools enforce those checks.
2. `ModelTurn` and `InvestigationModel` define the narrow model interface.
   `Protocol` describes the behavior required without forcing inheritance.
3. `tool_registry()` binds exactly six names to typed input schemas and already
   case-bound methods. There is no `eval` or model-controlled Python lookup.
   `tool_definitions()` converts those schemas into provider tool definitions;
   local schema references are expanded for compatibility. Runtime validation
   remains authoritative even if a provider ignores schema constraints.
4. `dispatch_tool()` rejects unknown names, malformed JSON, duplicate keys,
   nonfinite JSON values, non-object arguments, and oversized arguments. Existing
   tools then validate their Pydantic request schemas and case authorization.
   Validation errors return to the model so it can correct its next call.
5. `run_investigation()` authorizes the case BEFORE contacting the model and
   supplies the server's deterministic leverage summary. It sends a conversation,
   receives model-selected tool calls, validates the full call envelope, executes
   sequentially, and appends each result with its matching call ID. Multiple
   calls in one response count individually against the tool budget.
6. `AgentStep` records model usage and actual tool arguments/results. `on_step`
   is an optional local display callback; it must be fast. `AgentRunResult`
   contains the steps, counts, elapsed time, termination reason, and any outcome.

`app/agent_model.py` adapts the existing configured client. It sends tool schemas
and uses `tool_choice="auto"`, giving the model the choice. Automatic transport
retries are disabled, each request is bounded by 30 seconds or the remaining
run time, and generated output is capped at 2,048 tokens. A provider may use part
of that budget for thinking, so truncation is a possible explicit model failure.
The adapter preserves the provider assistant message, including opaque Gemini
metadata needed for later tool-calling turns. It does not request or display
private reasoning. No fallback model is selected automatically.

## Termination and timeout

Defaults: 12 model turns, 24 attempted tool calls, and a 120-second wall-clock
limit. Successful `create_finding`/`request_human_review` ends immediately, even
if more calls remain in the same model response. Terminal outcomes cannot be
replaced. Plain assistant text is not accepted as a finding.

Termination reasons are `draft_finding`, `human_review`, `step_limit`,
`tool_limit`, `timeout`, `scope_unavailable`, `model_error`,
`invalid_model_response`, `no_tool_call`, and `tool_error`. Unknown/malformed tool
arguments can be repaired; invalid provider envelopes and fatal scope/database
errors stop execution. Provider exception text is not exposed in the result.

For this LOCAL synchronous stage, a POSIX signal timer interrupts authorization,
model calls, tool calls, and display callbacks. Cancellation uses an internal
`BaseException` subclass so broad tool/provider `except Exception` handlers do
not swallow it. The prior signal handler is restored on exit. The runner refuses
to replace an active real-time timer and requires the main thread on macOS/Linux;
an unsupported execution context currently returns `tool_error`. This works in
the tested macOS command-line process and a normal main-thread notebook kernel,
but is not a threaded FastAPI/background-worker runner or a portable Windows
solution. A future worker design should use process isolation/cancellation.

A timer cannot retract requests already received by a provider. Callers must use
a dedicated session and roll it back/close it after the run, especially after
cancellation. The runner does not commit. No automatic retry, analyst notification,
resume, or claim-entailment verifier is implemented.

## Run it in a notebook

Restart your kernel first to avoid cached configuration. Select the application's
`.venv` kernel. The following reuses the REAL index from the previous embedding
run, not the offline fake-vector demo. Docker PostgreSQL must be running.

Setup (no model requests):

```python
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

APP_DIR = Path('/Users/yuezf/AI_Projects/Chat-with-PDF-RAG-System/rag-document-assistant')
sys.path.insert(0, str(APP_DIR))
assert (APP_DIR / '.env').is_file()
load_dotenv(APP_DIR / '.env', override=False)
os.environ['DATABASE_URL'] = (
    'postgresql+psycopg://rag_app:rag_app_local@127.0.0.1:55432/'
    'rag_credit_embedding_20261025_test'
)

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.agent_loop import run_investigation
from app.agent_model import ConfiguredInvestigationModel
from app.case_tools import CaseContext
from app.credit_fixture import local_database_url, seed_credit_fixture
from app.embedding_backend import ConfiguredEmbeddingBackend

engine = create_engine(local_database_url(os.environ['DATABASE_URL']))
with Session(engine) as session:
    scope = seed_credit_fixture(
        session, storage_root=Path('/private/tmp/credit-real-embedding-originals')
    )
    context = CaseContext(**scope.__dict__)

model = ConfiguredInvestigationModel()
backend = ConfiguredEmbeddingBackend()
print('Model:', model.model_name)
```

Execute (real chat calls and any model-selected query embeddings):

```python
def show_step(step):
    if step.kind == 'model':
        print(f'Model turn {step.number}: response received', flush=True)
    else:
        print(f'Tool {step.number}: {step.tool_name}', flush=True)
        print('Arguments:', step.arguments, flush=True)
        print('Success:', step.result['ok'], flush=True)

with Session(engine) as session:
    try:
        result = run_investigation(
            session, context, model, embeddings=backend,
            max_steps=12, max_tool_calls=24, timeout_seconds=120,
            on_step=show_step,
        )
    finally:
        session.rollback()

print('Termination:', result.termination_reason)
print('Model turns:', result.model_turns)
print('Tool calls:', result.tool_calls)
print('Seconds:', round(result.elapsed_seconds, 2))
if result.outcome is not None:
    print(result.outcome.model_dump_json(indent=2))
```

Inspect or export your trace (no additional model calls):

```python
for step in result.steps:
    if step.kind == 'tool':
        print(step.tool_name, step.result)

# Optional manual export; this is not database run persistence.
Path('/private/tmp/my-credit-agent-run.json').write_text(
    result.model_dump_json(indent=2)
)
engine.dispose()
```

Model choices can differ between runs. A successful draft still requires analyst
review: exact quote checks do not prove the causal claim or numeric equivalence.
The run is bounded, but repeated notebook executions create new API usage. Your
project's Free-tier availability and quotas still apply.

Live result: [October 26 smoke test](agent-run-2026-10-26.md).

## Tests and interview exercise

Unit tests: `.venv/bin/python -m unittest tests.test_agent_loop -v`.
PostgreSQL tests: `tests.test_agent_loop_integration`; select a newly migrated,
EMPTY local database ending `_test` and set `RUN_DATABASE_TESTS=1`. The suite
owns its seeded tenant and cleans it up. CI runs it first against the fresh DB.
Tests never call real models. They cover the full success path, irrelevant and
missing retrieval, fabricated quotes, unauthorized scope/arguments, malformed
JSON, duplicate call IDs, limits, provider errors, and model/tool cancellation.

Practice: implement a small `dispatch(name, arguments_json, registry)` in your
notebook. Return structured errors for an unknown name, invalid JSON, and
non-object arguments. Pass a dictionary to the selected function. Then add tests
proving an unknown name never invokes a function and a malformed request can be
followed by a corrected one. Compare with `dispatch_tool()` afterward.

Provider reference: [Gemini OpenAI-compatible function calling](https://ai.google.dev/gemini-api/docs/openai#function-calling).
