import hashlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.chunking import TextChunk
from app.document_repository import (
    add_chunks,
    add_document,
    add_pages,
    credit_scope_exists,
    delete_document_content,
    get_document_by_hash,
)
from app.embedding_utils import normalize_embedding
from app.ingestion import PageText
from app.models import Document, DocumentChunk, DocumentPage


@dataclass(frozen=True)
class DocumentPersistenceResult:
    """Summary of one atomic document-persistence operation."""

    document_id: uuid.UUID
    outcome: Literal["created", "skipped", "reprocessed"]
    page_count: int
    chunk_count: int


def build_document_id(tenant_id: uuid.UUID, content_hash: str) -> uuid.UUID:
    """Build a stable document ID from tenant ownership and file content."""
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"rag-credit:document:{tenant_id}:{content_hash.lower()}",
    )


def build_page_id(document_id: uuid.UUID, page_number: int) -> uuid.UUID:
    """Build a stable page ID within a document."""
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"rag-credit:page:{document_id}:{page_number}",
    )


def build_chunk_id(document_id: uuid.UUID, chunk_index: int) -> uuid.UUID:
    """Build a stable chunk ID within a document."""
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"rag-credit:chunk:{document_id}:{chunk_index}",
    )


def persist_document(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    borrower_id: uuid.UUID,
    reporting_period_id: uuid.UUID,
    original_filename: str,
    content_hash: str,
    byte_size: int,
    pages: Sequence[PageText],
    chunks: Sequence[TextChunk],
    embeddings: Sequence[Sequence[float]],
    embedding_model: str,
    media_type: str = "application/pdf",
    document_type: str = "filing",
    storage_uri: str | None = None,
    force_reprocess: bool = False,
) -> DocumentPersistenceResult:
    """Atomically persist a document, its pages, and its embedded chunks.

    This service owns the transaction. It commits a complete document or rolls
    back every change, including child deletion during forced reprocessing.
    """
    normalized_hash = content_hash.lower()

    try:
        _validate_document_metadata(
            original_filename=original_filename,
            content_hash=normalized_hash,
            byte_size=byte_size,
            embedding_model=embedding_model,
        )

        if not credit_scope_exists(
            session,
            tenant_id=tenant_id,
            borrower_id=borrower_id,
            reporting_period_id=reporting_period_id,
        ):
            raise ValueError(
                "Borrower and reporting period must belong to the supplied tenant"
            )

        existing_document = get_document_by_hash(
            session,
            tenant_id=tenant_id,
            content_hash=normalized_hash,
        )
        if existing_document is not None and not force_reprocess:
            session.commit()
            return DocumentPersistenceResult(
                document_id=existing_document.id,
                outcome="skipped",
                page_count=0,
                chunk_count=0,
            )

        normalized_embeddings = _validate_document_content(
            pages=pages,
            chunks=chunks,
            embeddings=embeddings,
        )

        if existing_document is None:
            document = Document(
                id=build_document_id(tenant_id, normalized_hash),
                tenant_id=tenant_id,
                borrower_id=borrower_id,
                reporting_period_id=reporting_period_id,
                original_filename=original_filename,
                content_hash=normalized_hash,
                media_type=media_type,
                document_type=document_type,
                byte_size=byte_size,
                storage_uri=storage_uri,
                status="processing",
            )
            add_document(session, document)
            outcome: Literal["created", "reprocessed"] = "created"
        else:
            if (
                existing_document.borrower_id != borrower_id
                or existing_document.reporting_period_id != reporting_period_id
            ):
                raise ValueError(
                    "An existing document cannot be reassigned to another borrower "
                    "or reporting period"
                )

            document = existing_document
            delete_document_content(session, document.id)
            document.original_filename = original_filename
            document.media_type = media_type
            document.document_type = document_type
            document.byte_size = byte_size
            document.storage_uri = storage_uri
            document.status = "processing"
            outcome = "reprocessed"

        page_rows = [
            DocumentPage(
                id=build_page_id(document.id, page.page_number),
                document_id=document.id,
                page_number=page.page_number,
                text_content=page.text,
                text_hash=hashlib.sha256(page.text.encode("utf-8")).hexdigest(),
                extraction_method="pypdf",
            )
            for page in pages
        ]
        add_pages(session, page_rows)

        chunk_rows = [
            DocumentChunk(
                id=build_chunk_id(document.id, chunk.chunk_index),
                document_id=document.id,
                chunk_index=chunk.chunk_index,
                text_content=chunk.text,
                start_page=chunk.start_page,
                end_page=chunk.end_page,
                embedding=embedding,
                embedding_model=embedding_model,
            )
            for chunk, embedding in zip(
                chunks,
                normalized_embeddings,
                strict=True,
            )
        ]
        add_chunks(session, chunk_rows)

        document.status = "completed"
        session.flush()
        session.commit()

        return DocumentPersistenceResult(
            document_id=document.id,
            outcome=outcome,
            page_count=len(page_rows),
            chunk_count=len(chunk_rows),
        )
    except Exception:
        session.rollback()
        raise


def _validate_document_metadata(
    *,
    original_filename: str,
    content_hash: str,
    byte_size: int,
    embedding_model: str,
) -> None:
    if not original_filename.strip():
        raise ValueError("original_filename is required")
    if re.fullmatch(r"[0-9a-f]{64}", content_hash) is None:
        raise ValueError("content_hash must be a 64-character SHA-256 hex digest")
    if byte_size < 0:
        raise ValueError("byte_size must be non-negative")
    if not embedding_model.strip():
        raise ValueError("embedding_model is required")


def _validate_document_content(
    *,
    pages: Sequence[PageText],
    chunks: Sequence[TextChunk],
    embeddings: Sequence[Sequence[float]],
) -> list[list[float]]:
    if len(chunks) != len(embeddings):
        raise ValueError("Each chunk must have exactly one embedding")

    page_numbers = [page.page_number for page in pages]
    if len(page_numbers) != len(set(page_numbers)):
        raise ValueError("Page numbers must be unique within a document")
    if any(page_number <= 0 for page_number in page_numbers):
        raise ValueError("Page numbers must be positive")

    available_pages = set(page_numbers)
    chunk_indexes = [chunk.chunk_index for chunk in chunks]
    if len(chunk_indexes) != len(set(chunk_indexes)):
        raise ValueError("Chunk indexes must be unique within a document")

    for chunk in chunks:
        if chunk.chunk_index < 0:
            raise ValueError("Chunk indexes must be non-negative")
        if chunk.start_page <= 0 or chunk.end_page < chunk.start_page:
            raise ValueError("Each chunk must have a valid positive page range")
        if (
            chunk.start_page not in available_pages
            or chunk.end_page not in available_pages
        ):
            raise ValueError("Each chunk page range must refer to persisted pages")

    return [normalize_embedding(embedding) for embedding in embeddings]
