# Agent engineering learning roadmap

This roadmap distinguishes implemented local stages from planned work. Development stays
local. Complete and review one small stage at a time, with an explanation of
choices and failure modes, appropriate tests, and an interview-style exercise.

## Existing foundation

The repository has a pure leverage calculation/rule, explicit fact measurement
windows, a repeatable synthetic case with page-linked facts, and an authorized
case evaluator. The calculation/rule result is returned in memory. The fixture
seeds pages without embeddings. An explicit adapter and one real
embedding smoke test now exist, as does the bounded local model/tool loop.
Database run persistence remains planned.

## 1. Typed tools before a model

Implemented locally: [fact/page read tools and walkthrough](case-read-tools-stage.md).
Search and terminal contracts are now implemented as stage 2 below; the model loop is implemented in stage 3 below.

Start with an immutable authorized case context and three tools:
`get_current_fact`, `get_prior_fact`, and `get_document_page`. Keep tenant,
borrower, and allowed periods server-bound. Define typed inputs, JSON-compatible
outputs, decimal strings, evidence references, and structured errors. Test normal
reads, missing data, malformed arguments, and cross-case access.

Practice: implement a fact-result serializer and explain which fields the model
can choose versus which come from the server. Review this stage before search.

## 2. Search and terminal outcomes

Implemented locally: [indexing, search, and outcomes walkthrough](search-outcomes-stage.md).
The backend is explicitly injected; offline fake-vector tests do not establish
semantic retrieval quality. No real backend or model loop is enabled by default.

Index the existing synthetic document and expose bounded, case-scoped
`search_documents`. Preserve source-page references: the existing forced
reprocessing path deletes pages and can clear fact foreign keys. Choose and
explain an indexing strategy before executing any paid embedding calls.

Add `create_finding` and `request_human_review` contracts. Validate referenced
pages, exact quotes, and scope; distinguish structural citation checks from
whether evidence actually supports a causal claim. Define draft finding and
escalation results without giving the model control over financial facts/rules.

Practice: reject an out-of-case citation with a useful structured error.

## 3. One explicit Python control loop

Implemented locally: [bounded loop and notebook walkthrough](bounded-agent-stage.md).
See its main-thread macOS/Linux timeout limitation and the recorded live smoke test.

Implement model request -> validate -> execute tool -> return result -> repeat.
Bound model/tool calls with step limits, a total deadline, individual timeouts,
and explicit finding, review, limit, timeout, and failure termination reasons.
Start with fake model responses; introduce real-model calls only with explicit
usage/cost controls. No framework or multi-agent orchestration is required here.

Practice: handle malformed arguments and explain how the loop terminates.

## 4. Trace persistence and evaluations

Persist the exact calculation inputs and rule result used for a run, its tool
calls/results, evidence references, and outcome. Snapshots must remain
reconstructable if canonical facts later change. Test success, insufficient
or irrelevant evidence, unauthorized scope, malformed arguments, and limits.
Record citation correctness, success/failure, latency, and cost on a small
real-model evaluation set after deterministic tests pass.

Practice: reconstruct a run from its stored steps and identify its termination.

## 5. LangGraph for a concrete workflow requirement

Reuse the same domain services, tool contracts, and evaluation cases. Represent
case/run context as graph state, operations as nodes, and routing as conditional
edges. Introduce persistent checkpoints and a human-review interrupt. An analyst
must be able to approve/reject or supply evidence after the worker restarts.

Test pause -> restart -> resume, correct case ownership on resume, continued
execution budgets, and idempotent writes. A resumed operation must not create a
duplicate finding. Graph checkpoints track execution state; application facts,
findings, and audit records retain their own domain persistence responsibilities.
Never treat a graph thread ID as authorization.

Practice: implement a conditional routing function and a resume regression test.

## 6. Focused LangChain learning extension

Use LangChain's model/tool interfaces or agent harness in a small adapter or
comparison. Reuse the existing tools and evaluation cases; do not rewrite working
PDF ingestion or credit services to satisfy a framework dependency. Explain
which message, schema, and model integrations it handles and which validation
remains application-owned. A second full application is not required.

Practice: adapt one existing tool and test that its authorization is preserved.

## Reference material

- [LangChain overview](https://docs.langchain.com/oss/python/langchain/overview)
- [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
