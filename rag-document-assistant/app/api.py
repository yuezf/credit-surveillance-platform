import hashlib
import shutil
import uuid
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import get_current_tenant_id
from app.chunking import chunking_across_pages
from app.configure import EMBEDDING_MODEL
from app.database import check_database_connection, get_db_session
from app.document_repository import credit_scope_exists, get_document_by_hash
from app.document_service import persist_document
from app.embedding_service import get_embeddings_in_batches
from app.ingestion import extract_pages_from_pdf
from app.models import Document, DocumentChunk, DocumentPage
from app.rag import generate_answer
from app.retrieval_service import search_similar_chunks


app = FastAPI(title="RAG Document Assistant")


class SearchRequest(BaseModel):
    queries: list[str]
    top_k: int = Field(gt=0, le=100)
    document_id: uuid.UUID | None = None
    borrower_id: uuid.UUID | None = None
    reporting_period_id: uuid.UUID | None = None
    max_distance: float | None = Field(default=0.6, ge=0)


class AskRequest(BaseModel):
    query: str
    top_k: int = Field(gt=0, le=100)
    document_id: uuid.UUID | None = None
    borrower_id: uuid.UUID | None = None
    reporting_period_id: uuid.UUID | None = None
    max_distance: float | None = Field(default=0.6, ge=0)


def compute_file_hash(file_path: str) -> str:
    """Return the SHA-256 hash of a file's original bytes."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            sha256.update(block)
    return sha256.hexdigest()


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness_check() -> dict[str, str]:
    try:
        check_database_connection()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Database is unavailable",
        ) from exc

    return {"status": "ready", "database": "ok"}


@app.post("/ingest")
def ingest_pdf(
    files: Annotated[list[UploadFile], File()],
    session: Annotated[Session, Depends(get_db_session)],
    tenant_id: Annotated[uuid.UUID, Depends(get_current_tenant_id)],
    borrower_id: Annotated[uuid.UUID, Form()],
    reporting_period_id: Annotated[uuid.UUID, Form()],
    chunk_size: Annotated[int, Form(gt=0)] = 500,
    overlap: Annotated[int, Form(ge=0)] = 50,
    force_reprocess: Annotated[bool, Form()] = False,
) -> dict[str, list[dict[str, Any]]]:
    if overlap >= chunk_size:
        raise HTTPException(
            status_code=400,
            detail="overlap must be smaller than chunk_size",
        )
    if not credit_scope_exists(
        session,
        tenant_id=tenant_id,
        borrower_id=borrower_id,
        reporting_period_id=reporting_period_id,
    ):
        raise HTTPException(
            status_code=400,
            detail="Unknown borrower/reporting-period combination",
        )

    results = []
    for file in files:
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDFs are supported")

        temp_path: str | None = None
        try:
            with NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
                shutil.copyfileobj(file.file, temp)
                temp_path = temp.name

            document_hash = compute_file_hash(temp_path)
            existing_document = get_document_by_hash(
                session,
                tenant_id=tenant_id,
                content_hash=document_hash,
            )
            if existing_document is not None and not force_reprocess:
                results.append(
                    {
                        "filename": file.filename,
                        "tenant_id": str(tenant_id),
                        "borrower_id": str(existing_document.borrower_id),
                        "reporting_period_id": str(
                            existing_document.reporting_period_id
                        ),
                        "document_id": str(existing_document.id),
                        "document_hash": document_hash,
                        "status": "skipped_already_ingested",
                    }
                )
                continue

            pages = extract_pages_from_pdf(temp_path)
            chunks = chunking_across_pages(pages, chunk_size, overlap)
            embeddings = get_embeddings_in_batches(
                [chunk.text for chunk in chunks]
            )
            persistence = persist_document(
                session,
                tenant_id=tenant_id,
                borrower_id=borrower_id,
                reporting_period_id=reporting_period_id,
                original_filename=file.filename,
                content_hash=document_hash,
                byte_size=Path(temp_path).stat().st_size,
                pages=pages,
                chunks=chunks,
                embeddings=embeddings,
                embedding_model=EMBEDDING_MODEL,
                force_reprocess=force_reprocess,
            )

            results.append(
                {
                    "filename": file.filename,
                    "tenant_id": str(tenant_id),
                    "borrower_id": str(borrower_id),
                    "reporting_period_id": str(reporting_period_id),
                    "document_id": str(persistence.document_id),
                    "document_hash": document_hash,
                    "status": (
                        "force_reprocessed"
                        if persistence.outcome == "reprocessed"
                        else "newly_ingested"
                    ),
                    "num_pages_with_text": persistence.page_count,
                    "num_chunks": persistence.chunk_count,
                    "preview": chunks[0].text[:100] if chunks else "",
                    "first_chunk_page_range": (
                        {
                            "start_page": chunks[0].start_page,
                            "end_page": chunks[0].end_page,
                            "page_span": chunks[0].page_span,
                        }
                        if chunks
                        else None
                    ),
                }
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail="Document ingestion failed") from exc
        finally:
            if temp_path is not None:
                Path(temp_path).unlink(missing_ok=True)

    return {"documents": results}


@app.get("/")
async def main() -> HTMLResponse:
    content = """
    <html>
        <body>
            <h2>Credit Surveillance Platform</h2>
            <p>Use <a href="/docs">API docs</a> with a tenant API key.</p>
        </body>
    </html>
    """
    return HTMLResponse(content=content)


@app.post("/search")
def retrieve_documents(
    request: SearchRequest,
    session: Annotated[Session, Depends(get_db_session)],
    tenant_id: Annotated[uuid.UUID, Depends(get_current_tenant_id)],
) -> dict[str, dict[str, Any]]:
    try:
        result = {}
        for index, query in enumerate(request.queries):
            matches = search_similar_chunks(
                session,
                query=query,
                top_k=request.top_k,
                tenant_id=tenant_id,
                document_id=request.document_id,
                borrower_id=request.borrower_id,
                reporting_period_id=request.reporting_period_id,
                max_distance=request.max_distance,
            )
            result[f"query-{index}"] = {
                "query": query,
                "top_k": request.top_k,
                "tenant_id": str(tenant_id),
                "document_id": (
                    str(request.document_id)
                    if request.document_id is not None
                    else None
                ),
                "result": matches,
            }
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Document search failed") from exc


@app.post("/answer")
def get_answer_from_llm(
    request: AskRequest,
    session: Annotated[Session, Depends(get_db_session)],
    tenant_id: Annotated[uuid.UUID, Depends(get_current_tenant_id)],
) -> dict[str, Any]:
    try:
        return generate_answer(
            session,
            query=request.query,
            top_k=request.top_k,
            tenant_id=tenant_id,
            document_id=request.document_id,
            borrower_id=request.borrower_id,
            reporting_period_id=request.reporting_period_id,
            max_distance=request.max_distance,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Answer generation failed") from exc


@app.get("/collection-info")
def collection_info(
    session: Annotated[Session, Depends(get_db_session)],
    tenant_id: Annotated[uuid.UUID, Depends(get_current_tenant_id)],
) -> dict[str, Any]:
    document_count = session.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.tenant_id == tenant_id)
    )
    page_count = session.scalar(
        select(func.count())
        .select_from(DocumentPage)
        .join(Document, Document.id == DocumentPage.document_id)
        .where(Document.tenant_id == tenant_id)
    )
    chunk_count = session.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.tenant_id == tenant_id)
    )
    return {
        "tenant_id": str(tenant_id),
        "document_count": document_count,
        "page_count": page_count,
        "chunk_count": chunk_count,
    }
