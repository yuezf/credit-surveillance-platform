import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.configure import LLM_MODEL
from app.model_client import client
from app.retrieval_service import search_similar_chunks


def format_page_range(metadata: dict[str, Any]) -> str:
    """Turn a chunk's page range into a human-readable string."""
    start_page = metadata.get("start_page")
    end_page = metadata.get("end_page")

    if start_page is None or end_page is None:
        return "unknown page"
    if start_page == end_page:
        return f"page {start_page}"
    return f"pages {start_page}-{end_page}"


def generate_answer(
    session: Session,
    *,
    query: str,
    top_k: int,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID | None = None,
    borrower_id: uuid.UUID | None = None,
    reporting_period_id: uuid.UUID | None = None,
    max_distance: float | None = 0.6,
) -> dict[str, Any]:
    """Generate an answer from tenant-scoped PostgreSQL retrieval results."""
    retrieved_chunks = search_similar_chunks(
        session,
        query=query,
        top_k=top_k,
        tenant_id=tenant_id,
        document_id=document_id,
        borrower_id=borrower_id,
        reporting_period_id=reporting_period_id,
        max_distance=max_distance,
    )
    context_blocks = []
    sources = []

    for source_id, chunk in enumerate(retrieved_chunks, start=1):
        metadata = chunk["metadata"]
        page_range = format_page_range(metadata)

        context_blocks.append(
            f"""Source {source_id}
Document: {metadata["document_name"]}
Document ID: {metadata["document_id"]}
Location: {page_range}
Chunk index: {metadata["chunk_index"]}

{chunk["text"]}"""
        )
        sources.append(
            {
                "source_id": source_id,
                "document_name": metadata["document_name"],
                "document_id": metadata["document_id"],
                "chunk_index": metadata["chunk_index"],
                "start_page": metadata.get("start_page"),
                "end_page": metadata.get("end_page"),
                "page_span": metadata.get("page_span"),
                "text_preview": chunk["text"][:100],
                "distance": chunk["distance"],
            }
        )

    context = "\n\n".join(context_blocks)
    prompt = f"""
You are a helpful document assistant.

Answer the user's question using ONLY the context below.

If the context does not contain enough information, say:
"I don't have enough information in the uploaded documents to answer that."

When possible, mention the source number you use to support your points.

Context:
{context}

Question:
{query}

Answer:
"""

    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
    )

    return {
        "query": query,
        "tenant_id": str(tenant_id),
        "document_id": str(document_id) if document_id is not None else None,
        "answer": response.choices[0].message.content,
        "sources": sources,
    }
