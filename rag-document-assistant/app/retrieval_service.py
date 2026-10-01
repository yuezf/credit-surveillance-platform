import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.document_repository import search_similar_chunks as search_postgres_chunks
from app.embedding_service import get_embeddings


def search_similar_chunks(
    session: Session,
    *,
    query: str,
    tenant_id: uuid.UUID,
    top_k: int,
    document_id: uuid.UUID | None = None,
    borrower_id: uuid.UUID | None = None,
    reporting_period_id: uuid.UUID | None = None,
    max_distance: float | None = 0.6,
) -> list[dict[str, Any]]:
    """Embed a query and return PostgreSQL results in the RAG response shape."""
    if not query.strip():
        raise ValueError("query is required")

    query_embedding = get_embeddings([query])[0]
    matches = search_postgres_chunks(
        session,
        tenant_id=tenant_id,
        query_embedding=query_embedding,
        top_k=top_k,
        document_id=document_id,
        borrower_id=borrower_id,
        reporting_period_id=reporting_period_id,
        max_distance=max_distance,
    )

    return [
        {
            "id": str(match.id),
            "text": match.text,
            "metadata": {
                "tenant_id": str(tenant_id),
                "document_id": str(match.document_id),
                "document_name": match.document_name,
                "document_hash": match.document_hash,
                "chunk_index": match.chunk_index,
                "start_page": match.start_page,
                "end_page": match.end_page,
                "page_span": list(range(match.start_page, match.end_page + 1)),
            },
            "distance": match.distance,
        }
        for match in matches
    ]
