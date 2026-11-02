# Stage 3C: configured embedding adapter

`app/embedding_backend.py` connects the existing embedding service to the
case-tool interface. This stage adds no API endpoint, schema, provider, or model
loop. Its tests use mocks; implementation did not make a real embedding request.

## Follow the data flow

```text
index_case_document / InvestigationTools.search_documents
    -> embed_validated(backend, texts)
    -> ConfiguredEmbeddingBackend.embed(texts)
    -> existing get_embeddings_in_batches(texts)
    -> existing get_embeddings(texts)
    -> configured provider
```

Importing the adapter does not import configuration or construct the client.
Constructing it loads the existing configuration, checks the model identifier
and the database's required 768 dimensions, then captures the existing batch
function. Construction makes no embedding request. The current configuration
loader still requires all its usual settings, including LLM_MODEL and DATABASE_URL.

`model_name` is a read-only property used to label indexed chunks and filter
searches. `embed(texts)` returns immediately for an empty list; otherwise it
uses the existing batching and normalization. Case callers use `embed_validated`
to verify vector count/dimensions/values and return generic provider errors.
Calling the adapter directly does not provide that error-redaction boundary.
The adapter inherits the existing client's timeout/retry behavior; it adds no
investigation-wide time limit. That belongs to the later bounded-loop stage.

The class satisfies `EmbeddingBackend` by having the required attribute and
method; it does not need to inherit from the Protocol. This is structural typing:
the caller needs the behavior, not a particular parent class.

## Wiring for the later real run

With an authorized `context`, a seeded document, and a dedicated session:

```python
from app.embedding_backend import ConfiguredEmbeddingBackend
from app.credit_indexing import index_case_document
from app.investigation_tools import InvestigationTools

backend = ConfiguredEmbeddingBackend()  # Loads configuration; no API request.

# The following operations can send text to the provider and incur charges.
try:
    indexed = index_case_document(session, context, document.id, backend)
    session.commit()
except Exception:
    session.rollback()
    raise

tools = InvestigationTools(session, context, embeddings=backend)
search = tools.search_documents({"query": "Why did EBITDA decline?", "top_k": 3})
```

Use the SAME backend for both operations. Start with a separate local demo
database containing the fixture and no fake vectors. The earlier offline demo
may already contain a fake index; the indexer deliberately refuses to overwrite
it with a different model. Do not delete that index as part of this stage.
Changing environment settings requires restarting the process/kernel because
Python caches imported configuration. A model name identifies the configured
encoder but cannot detect a provider silently changing that encoder behind the
same name. Real retrieval quality is still unmeasured.

## Tests and practice

Run from `rag-document-assistant`:

```sh
.venv/bin/python -m unittest tests.test_embedding_backend tests.test_embedding_service tests.test_investigation_tools -v
```

Tests cover credential-free import, construction without embedding calls,
batch-service delegation and ordering, empty input, incompatible configuration,
and malformed vectors/provider failures through the case boundary. No live API
or PostgreSQL connection is needed for these tests.

Interview exercise: implement `embed_in_batches(texts, embed_batch, batch_size)`
in your notebook. Reject a nonpositive batch size, return an empty list without
calling the backend for empty input, and preserve input order. With five inputs
and batch size two, verify calls contain 2, 2, and 1 inputs. Compare your solution
with the existing `get_embeddings_in_batches` afterward. Explain why validating
the output count matters before pairing vectors with document chunks.
