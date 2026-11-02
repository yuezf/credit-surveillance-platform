"""Case search and in-memory draft/review outcomes shared by manual and model callers."""

import math
import uuid
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.case_tools import (
    CaseContext, CaseReadTools, FactOutput, PageOutput, PageReference, ToolModel,
    ToolResult, _ReadError, _failure, _page_reference,
)
from app.credit_indexing import EmbeddingBackend, EmbeddingError, embed_validated, embedding_model_name
from app.credit_service import CreditDataError, evaluate_credit_case
from app.models import Document, DocumentChunk, DocumentPage


ShortText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=1200)]
QuoteText = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=2000)]


class SearchRequest(ToolModel):
    query: Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)]
    top_k: int = Field(default=3, strict=True, ge=1, le=5)


class SearchHit(ToolModel):
    chunk_id: uuid.UUID
    text: str
    truncated: bool
    distance: float
    sources: tuple[PageReference, ...]


class SearchOutput(ToolModel):
    query: str
    embedding_model: str
    matches: tuple[SearchHit, ...]


class CitationRequest(ToolModel):
    document_id: uuid.UUID
    page_number: int = Field(strict=True, gt=0)
    quote: QuoteText


class ClaimRequest(ToolModel):
    text: ShortText
    citations: list[CitationRequest] = Field(min_length=1, max_length=5)


class FindingRequest(ToolModel):
    claims: list[ClaimRequest] = Field(min_length=1, max_length=5)


class HumanReviewRequest(ToolModel):
    reason: ShortText
    unresolved_questions: list[ShortText] = Field(default_factory=list, max_length=5)


class ValidatedCitation(ToolModel):
    source: PageReference
    quote: str


class CitedClaim(ToolModel):
    text: str
    citations: tuple[ValidatedCitation, ...]


class DeterministicRuleSummary(ToolModel):
    prior_leverage: str
    current_leverage: str
    threshold: str
    severity: Literal["critical"]


class DraftFinding(ToolModel):
    status: Literal["draft_finding"] = "draft_finding"
    borrower_id: uuid.UUID
    prior_period_id: uuid.UUID
    current_period_id: uuid.UUID
    claims: tuple[CitedClaim, ...]
    rule: DeterministicRuleSummary
    citation_validation: Literal["passed"] = "passed"
    requires_human_review: Literal[True] = True


class HumanReview(ToolModel):
    status: Literal["human_review"] = "human_review"
    borrower_id: uuid.UUID
    prior_period_id: uuid.UUID
    current_period_id: uuid.UUID
    reason: str
    unresolved_questions: tuple[str, ...]


class InvestigationTools(CaseReadTools):
    """One sequential investigation per instance; state is NOT persisted yet.

    The caller owns the session/transaction. Provide an embedding backend only
    when search is wanted. It must be the same encoder/model used for indexing.
    """

    def __init__(self, session, context: CaseContext, *, embeddings: EmbeddingBackend | None = None, max_distance: float = 0.6):
        super().__init__(session, context)
        if not math.isfinite(max_distance) or not 0 <= max_distance <= 2:
            raise ValueError("max_distance must be finite and between 0 and 2")
        self._embeddings = embeddings
        self._max_distance = max_distance
        self._inspected_pages: dict[tuple[uuid.UUID, int], PageReference] = {}
        self._outcome: DraftFinding | HumanReview | None = None

    @property
    def outcome(self) -> DraftFinding | HumanReview | None:
        return self._outcome

    def _closed(self):
        if self._outcome is not None:
            return _failure("already_terminated", "This investigation already has a terminal outcome")
        return None

    def _pages(self):
        # Refresh any ORM objects still cached after an earlier page inspection.
        return super()._pages().execution_options(populate_existing=True)

    def get_current_fact(self, arguments: dict[str, Any]) -> ToolResult[FactOutput]:
        closed = self._closed()
        return closed if closed is not None else super().get_current_fact(arguments)

    def get_prior_fact(self, arguments: dict[str, Any]) -> ToolResult[FactOutput]:
        closed = self._closed()
        return closed if closed is not None else super().get_prior_fact(arguments)

    def get_document_page(self, arguments: dict[str, Any]) -> ToolResult[PageOutput]:
        closed = self._closed()
        if closed is not None:
            return closed
        result = super().get_document_page(arguments)
        if result.ok:
            reference = result.data.source
            self._inspected_pages[(reference.document_id, reference.page_number)] = reference
        return result

    def _chunks(self):
        scope = self._context
        return select(DocumentChunk, Document).join(
            Document, Document.id == DocumentChunk.document_id
        ).where(
            Document.tenant_id == scope.tenant_id,
            Document.borrower_id == scope.borrower_id,
            Document.reporting_period_id.in_((scope.prior_period_id, scope.current_period_id)),
            Document.status == "completed",
            DocumentChunk.embedding_model == embedding_model_name(self._embeddings),
        )

    def search_documents(self, arguments: dict[str, Any]) -> ToolResult[SearchOutput]:
        closed = self._closed()
        if closed is not None:
            return closed
        try:
            request = SearchRequest.model_validate(arguments)
        except ValidationError:
            return _failure("invalid_arguments", "Supply a nonempty query and top_k between 1 and 5 only")
        try:
            with self._session.no_autoflush:
                self._authorize()
                if self._embeddings is None:
                    return _failure("embedding_unavailable", "No embedding backend is configured for this investigation")
                statement = self._chunks()
                if self._session.execute(statement.limit(1)).first() is None:
                    return _failure("index_unavailable", "No matching embedding index is available within this case")
                vector = embed_validated(self._embeddings, [request.query])[0]
                distance = DocumentChunk.embedding.cosine_distance(vector)
                rows = self._session.execute(statement.add_columns(distance.label("distance")).where(
                    distance < self._max_distance
                ).order_by(distance, DocumentChunk.id).limit(request.top_k)).all()
                hits = []
                for chunk, document, score in rows:
                    if not 0 < chunk.end_page - chunk.start_page + 1 <= 10:
                        raise _ReadError("invalid_evidence", "Search match has an invalid page span")
                    pages = self._session.execute(self._pages().where(
                        Document.id == document.id,
                        DocumentPage.page_number.between(chunk.start_page, chunk.end_page),
                    ).order_by(DocumentPage.page_number)).all()
                    if [page.page_number for page, _ in pages] != list(range(chunk.start_page, chunk.end_page + 1)):
                        raise _ReadError("invalid_evidence", "Search match has missing source pages")
                    for page, _ in pages:
                        self._check_page(page)
                    if chunk.text_content not in "\n\n".join(page.text_content for page, _ in pages):
                        raise _ReadError("invalid_evidence", "Search text no longer matches its source pages")
                    hits.append(SearchHit(
                        chunk_id=chunk.id, text=chunk.text_content[:1500],
                        truncated=len(chunk.text_content) > 1500, distance=float(score),
                        sources=tuple(_page_reference(page, doc) for page, doc in pages),
                    ))
                return ToolResult[SearchOutput](ok=True, data=SearchOutput(
                    query=request.query, embedding_model=embedding_model_name(self._embeddings), matches=tuple(hits),
                ))
        except (PermissionError, CreditDataError):
            return _failure("scope_unavailable", "Case scope is unavailable or invalid")
        except EmbeddingError:
            return _failure("embedding_unavailable", "Embedding generation could not be completed")
        except _ReadError as exc:
            return _failure(exc.code, str(exc))
        except DBAPIError:
            return _failure("data_unavailable", "The database read could not be completed")

    def create_finding(self, arguments: dict[str, Any]) -> ToolResult[DraftFinding]:
        closed = self._closed()
        if closed is not None:
            return closed
        try:
            request = FindingRequest.model_validate(arguments)
        except ValidationError:
            return _failure("invalid_arguments", "Supply 1 to 5 claims, each with 1 to 5 page citations")
        try:
            with self._session.no_autoflush:
                self._authorize()
                claims = []
                for claim in request.claims:
                    citations = []
                    for citation in claim.citations:
                        row = self._session.execute(self._pages().where(
                            Document.id == citation.document_id,
                            DocumentPage.page_number == citation.page_number,
                        )).one_or_none()
                        if row is None:
                            return _failure("invalid_citation", "Citation page is unavailable within this case")
                        page, document = row
                        self._check_page(page)
                        current = _page_reference(page, document)
                        inspected = self._inspected_pages.get((document.id, page.page_number))
                        if inspected is None:
                            return _failure("evidence_not_inspected", "Fetch the exact citation page before creating a finding")
                        if current != inspected:
                            return _failure("invalid_citation", "Citation page changed since it was inspected; fetch it again")
                        if citation.quote not in page.text_content:
                            return _failure("invalid_citation", "Citation quote is absent from the inspected page")
                        citations.append(ValidatedCitation(source=current, quote=citation.quote))
                    claims.append(CitedClaim(text=claim.text, citations=tuple(citations)))
                try:
                    case = evaluate_credit_case(self._session, **self._context.model_dump())
                except CreditDataError:
                    return _failure("case_not_evaluable", "The financial case cannot currently be evaluated")
                if case.prior.calculation.ratio is None or case.current.calculation.ratio is None:
                    return _failure("case_not_evaluable", "The financial case cannot currently be evaluated")
                if not case.current.breached:
                    return _failure("no_critical_exception", "The deterministic rule reports no current critical exception")
                outcome = DraftFinding(
                    borrower_id=self._context.borrower_id,
                    prior_period_id=self._context.prior_period_id,
                    current_period_id=self._context.current_period_id,
                    claims=tuple(claims), rule=DeterministicRuleSummary(
                        prior_leverage=str(case.prior.calculation.ratio),
                        current_leverage=str(case.current.calculation.ratio),
                        threshold=str(case.current.threshold), severity="critical",
                    ),
                )
                self._outcome = outcome
                return ToolResult[DraftFinding](ok=True, data=outcome)
        except (PermissionError, CreditDataError):
            return _failure("scope_unavailable", "Case scope is unavailable or invalid")
        except _ReadError as exc:
            return _failure(exc.code, str(exc))
        except DBAPIError:
            return _failure("data_unavailable", "The database read could not be completed")

    def request_human_review(self, arguments: dict[str, Any]) -> ToolResult[HumanReview]:
        closed = self._closed()
        if closed is not None:
            return closed
        try:
            request = HumanReviewRequest.model_validate(arguments)
        except ValidationError:
            return _failure("invalid_arguments", "Supply a reason and up to 5 unresolved questions")
        try:
            with self._session.no_autoflush:
                self._authorize()
                outcome = HumanReview(
                    borrower_id=self._context.borrower_id,
                    prior_period_id=self._context.prior_period_id,
                    current_period_id=self._context.current_period_id,
                    reason=request.reason, unresolved_questions=tuple(request.unresolved_questions),
                )
                self._outcome = outcome
                return ToolResult[HumanReview](ok=True, data=outcome)
        except (PermissionError, CreditDataError):
            return _failure("scope_unavailable", "Case scope is unavailable or invalid")
        except DBAPIError:
            return _failure("data_unavailable", "The database read could not be completed")
