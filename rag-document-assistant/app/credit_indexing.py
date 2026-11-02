"""Add a case document's vector index without replacing provenance pages.

The caller explicitly supplies an embedding backend and owns commit/rollback.
No backend is selected from environment variables and no model call is implicit.
"""

import hashlib
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.case_tools import CaseContext
from app.chunking import chunking_across_pages
from app.credit_service import authorize_credit_case
from app.document_service import build_chunk_id
from app.embedding_utils import normalize_embedding
from app.ingestion import PageText
from app.models import Document, DocumentChunk, DocumentPage


class EmbeddingBackend(Protocol):
    model_name: str

    def embed(self, texts: list[str]) -> Sequence[Sequence[float]]: ...


class EmbeddingError(ValueError):
    """Backend failure or invalid vectors; messages contain no provider details."""


class IndexConflictError(ValueError):
    """Existing index/pages cannot safely be reused. Nothing is deleted."""


def embedding_model_name(backend: EmbeddingBackend) -> str:
    name = backend.model_name
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise EmbeddingError("Embedding backend requires a nonempty model identifier")
    return name


def embed_validated(backend: EmbeddingBackend, texts: list[str]) -> list[list[float]]:
    embedding_model_name(backend)
    try:
        vectors = list(backend.embed(texts))
        if len(vectors) != len(texts):
            raise ValueError("Wrong embedding count")
        normalized = [normalize_embedding(vector) for vector in vectors]
        if any(not any(vector) or not all(math.isfinite(v) for v in vector) for vector in normalized):
            raise ValueError("Invalid normalized vector")
        return normalized
    except Exception as exc:
        raise EmbeddingError("Embedding generation failed or returned invalid vectors") from exc


@dataclass(frozen=True)
class IndexResult:
    document_id: uuid.UUID
    embedding_model: str
    chunk_count: int
    reused: bool


def index_case_document(
    session: Session,
    context: CaseContext,
    document_id: uuid.UUID,
    backend: EmbeddingBackend,
) -> IndexResult:
    """Flush chunks/status in the caller's transaction; NEVER delete pages.

    Use a dedicated session and transaction. A matching index is a no-op; a
    different model/layout is an explicit conflict, not an implicit reindex.
    Row locks protect the selected document/pages while indexing takes place.
    """
    context = CaseContext.model_validate(context)
    authorize_credit_case(session, **context.model_dump())
    document = session.scalar(select(Document).where(
        Document.id == document_id,
        Document.tenant_id == context.tenant_id,
        Document.borrower_id == context.borrower_id,
        Document.reporting_period_id.in_((context.prior_period_id, context.current_period_id)),
    ).with_for_update())
    if document is None:
        raise PermissionError("Document is unavailable within this case")
    if document.status not in ("pending", "completed"):
        raise IndexConflictError("Document is not ready for indexing")
    model_name = embedding_model_name(backend)
    pages = session.scalars(select(DocumentPage).where(
        DocumentPage.document_id == document.id
    ).order_by(DocumentPage.page_number).with_for_update()).all()
    if not pages:
        raise IndexConflictError("Document has no extracted pages")
    chunks = []
    for page in pages:
        if not page.text_content.strip() or hashlib.sha256(page.text_content.encode()).hexdigest() != page.text_hash:
            raise IndexConflictError("Stored page text is empty or inconsistent with its hash")
        chunks.extend(chunking_across_pages(
            [PageText(page_number=page.page_number, text=page.text_content)],
            chunk_size=800, overlap=100,
        ))
    existing = session.scalars(select(DocumentChunk).where(
        DocumentChunk.document_id == document.id
    ).order_by(DocumentChunk.chunk_index)).all()
    if existing:
        if len(existing) != len(chunks):
            raise IndexConflictError("Existing index has a different chunk layout")
        for index, (row, chunk) in enumerate(zip(existing, chunks, strict=True)):
            if (
                row.chunk_index != index or row.embedding_model != model_name
                or row.text_content != chunk.text
                or row.start_page != chunk.start_page or row.end_page != chunk.end_page
            ):
                raise IndexConflictError("Existing index uses different content, layout, or embedding model")
            normalize_embedding(row.embedding)
        if document.status != "completed":
            document.status = "completed"
            session.flush()
        return IndexResult(document.id, model_name, len(existing), reused=True)
    vectors = embed_validated(backend, [chunk.text for chunk in chunks])
    session.add_all([
        DocumentChunk(
            id=build_chunk_id(document.id, index), document_id=document.id,
            chunk_index=index, text_content=chunk.text,
            start_page=chunk.start_page, end_page=chunk.end_page,
            embedding=vector, embedding_model=model_name,
        )
        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
    ])
    document.status = "completed"
    session.flush()
    return IndexResult(document.id, model_name, len(chunks), reused=False)
