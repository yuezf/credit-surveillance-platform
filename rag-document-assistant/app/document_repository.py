import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.embedding_utils import normalize_embedding
from app.models import Borrower, Document, DocumentChunk, DocumentPage, ReportingPeriod


@dataclass(frozen=True)
class ChunkSearchResult:
    """A tenant-scoped chunk match returned by PostgreSQL/pgvector."""

    id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    document_hash: str
    chunk_index: int
    text: str
    start_page: int
    end_page: int
    distance: float


def credit_scope_exists(
    session: Session,
    tenant_id: uuid.UUID,
    borrower_id: uuid.UUID,
    reporting_period_id: uuid.UUID,
) -> bool:
    """Return whether the borrower and period belong to the supplied tenant."""
    statement = (
        select(ReportingPeriod.id)
        .join(Borrower, Borrower.id == ReportingPeriod.borrower_id)
        .where(
            Borrower.id == borrower_id,
            Borrower.tenant_id == tenant_id,
            ReportingPeriod.id == reporting_period_id,
            ReportingPeriod.tenant_id == tenant_id,
            ReportingPeriod.borrower_id == borrower_id,
        )
    )
    return session.scalar(statement) is not None


def get_document_by_id(
    session: Session,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
) -> Document | None:
    """Return one document only when it belongs to the supplied tenant."""
    statement = select(Document).where(
        Document.id == document_id,
        Document.tenant_id == tenant_id,
    )
    return session.scalar(statement)


def get_document_by_hash(
    session: Session,
    tenant_id: uuid.UUID,
    content_hash: str,
) -> Document | None:
    """Return a tenant's document with the given content hash, if it exists."""
    statement = select(Document).where(
        Document.tenant_id == tenant_id,
        Document.content_hash == content_hash,
    )
    return session.scalar(statement)


def add_document(session: Session, document: Document) -> Document:
    """Stage a document insert and flush it without committing the transaction."""
    session.add(document)
    session.flush()
    return document


def add_pages(
    session: Session,
    pages: Sequence[DocumentPage],
) -> None:
    """Stage page inserts and flush them without committing the transaction."""
    session.add_all(pages)
    session.flush()


def add_chunks(
    session: Session,
    chunks: Sequence[DocumentChunk],
) -> None:
    """Stage chunk inserts and flush them without committing the transaction."""
    session.add_all(chunks)
    session.flush()


def delete_document_content(
    session: Session,
    document_id: uuid.UUID,
) -> None:
    """Delete a document's derived chunks and pages while keeping the document."""
    session.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
    )
    session.execute(
        delete(DocumentPage).where(DocumentPage.document_id == document_id)
    )
    session.flush()


def get_document_page(
    session: Session,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    page_number: int,
) -> DocumentPage | None:
    """Return an exact page after verifying the document's tenant scope."""
    statement = (
        select(DocumentPage)
        .join(Document, Document.id == DocumentPage.document_id)
        .where(
            Document.tenant_id == tenant_id,
            DocumentPage.document_id == document_id,
            DocumentPage.page_number == page_number,
        )
    )
    return session.scalar(statement)


def search_similar_chunks(
    session: Session,
    tenant_id: uuid.UUID,
    query_embedding: Sequence[float],
    top_k: int,
    document_id: uuid.UUID | None = None,
    borrower_id: uuid.UUID | None = None,
    reporting_period_id: uuid.UUID | None = None,
    max_distance: float | None = 0.6,
) -> list[ChunkSearchResult]:
    """Search document chunks with cosine distance inside one tenant."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if max_distance is not None and max_distance < 0:
        raise ValueError("max_distance must be non-negative or None")

    normalized_query = normalize_embedding(query_embedding)
    distance = DocumentChunk.embedding.cosine_distance(normalized_query)

    statement = (
        select(
            DocumentChunk,
            Document.original_filename,
            Document.content_hash,
            distance.label("distance"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.tenant_id == tenant_id)
    )

    if document_id is not None:
        statement = statement.where(Document.id == document_id)
    if borrower_id is not None:
        statement = statement.where(Document.borrower_id == borrower_id)
    if reporting_period_id is not None:
        statement = statement.where(
            Document.reporting_period_id == reporting_period_id
        )
    if max_distance is not None:
        statement = statement.where(distance < max_distance)

    statement = statement.order_by(distance).limit(top_k)
    rows = session.execute(statement).all()

    return [
        ChunkSearchResult(
            id=chunk.id,
            document_id=chunk.document_id,
            document_name=document_name,
            document_hash=document_hash,
            chunk_index=chunk.chunk_index,
            text=chunk.text_content,
            start_page=chunk.start_page,
            end_page=chunk.end_page,
            distance=float(chunk_distance),
        )
        for chunk, document_name, document_hash, chunk_distance in rows
    ]
