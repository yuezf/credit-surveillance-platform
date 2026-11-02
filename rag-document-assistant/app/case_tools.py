"""Three read-only case tools. No model client, framework, or credentials needed.

Trusted server code constructs CaseContext from authenticated identity and an
approved borrower/period pair. A model may supply ONLY the request arguments.
The caller owns the Session; tools never commit, roll back, or autoflush writes.
"""

import calendar
import hashlib
import re
import uuid
from datetime import date, timedelta
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.credit_service import CreditDataError, authorize_credit_case
from app.models import Document, DocumentPage, FinancialFact, ReportingPeriod


Metric = Literal["revenue", "total_debt", "ebitda", "interest_expense"]
ErrorCode = Literal[
    "invalid_arguments", "scope_unavailable", "fact_not_found", "page_not_found",
    "invalid_fact", "invalid_evidence", "data_unavailable",
    "index_unavailable", "embedding_unavailable", "invalid_citation",
    "evidence_not_inspected", "case_not_evaluable", "no_critical_exception",
    "already_terminated",
]


class ToolModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")


class CaseContext(ToolModel):
    """Server-bound scope, not part of a model-facing tool argument schema."""

    model_config = ConfigDict(strict=True)
    tenant_id: uuid.UUID
    borrower_id: uuid.UUID
    prior_period_id: uuid.UUID
    current_period_id: uuid.UUID


class FactRequest(ToolModel):
    metric: Metric


class PageRequest(ToolModel):
    document_id: uuid.UUID
    page_number: int = Field(strict=True, gt=0)


class PageReference(ToolModel):
    document_id: uuid.UUID
    document_name: str
    document_hash: str
    borrower_id: uuid.UUID
    reporting_period_id: uuid.UUID
    page_id: uuid.UUID
    page_number: int
    text_hash: str


class FactOutput(ToolModel):
    fact_id: uuid.UUID
    borrower_id: uuid.UUID
    reporting_period_id: uuid.UUID
    metric: Metric
    value: str
    currency: str
    scale: Literal["units", "millions"]
    basis: Literal["point_in_time", "ttm"]
    measurement_start: date | None
    measurement_end: date
    is_canonical: Literal[True] = True
    source_text: str
    source: PageReference


class PageOutput(ToolModel):
    source: PageReference
    text: str


class ToolError(ToolModel):
    code: ErrorCode
    message: str


Data = TypeVar("Data")


class ToolResult(ToolModel, Generic[Data]):
    ok: bool
    data: Data | None = None
    error: ToolError | None = None

    @model_validator(mode="after")
    def consistent_outcome(self):
        if self.ok and (self.data is None or self.error is not None):
            raise ValueError("Successful tool results require data and no error")
        if not self.ok and (self.data is not None or self.error is None):
            raise ValueError("Failed tool results require an error and no data")
        return self


class _ReadError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        self.code = code
        super().__init__(message)


def _failure(code: ErrorCode, message: str) -> ToolResult[Any]:
    return ToolResult(ok=False, error=ToolError(code=code, message=message))


def _page_reference(page: DocumentPage, document: Document) -> PageReference:
    return PageReference(
        document_id=document.id, document_name=document.original_filename,
        document_hash=document.content_hash, borrower_id=document.borrower_id,
        reporting_period_id=document.reporting_period_id, page_id=page.id,
        page_number=page.page_number, text_hash=page.text_hash,
    )


def serialize_fact(
    fact: FinancialFact, page: DocumentPage, document: Document
) -> FactOutput:
    """Serialize already-authorized, validated data; no database operations.

    format(..., 'f') preserves the Decimal value without a float conversion.
    Use result.model_dump(mode='json') for UUID/date strings at the tool boundary.
    """
    return FactOutput(
        fact_id=fact.id, borrower_id=fact.borrower_id,
        reporting_period_id=fact.reporting_period_id, metric=fact.metric_name,
        value=format(fact.value, "f"), currency=fact.currency, scale=fact.unit,
        basis=fact.measurement_basis, measurement_start=fact.measurement_start,
        measurement_end=fact.measurement_end, source_text=fact.source_text,
        source=_page_reference(page, document),
    )


def _validate_fact(fact: FinancialFact, period: ReportingPeriod) -> None:
    """Require explicit quarter-end/TTM semantics without deciding credit rules."""
    end = period.period_end
    if (
        not fact.value.is_finite()
        or fact.unit not in ("units", "millions")
        or not isinstance(fact.currency, str)
        or re.fullmatch(r"[A-Z]{3}", fact.currency) is None
        or fact.measurement_end != end
        or end.year <= 1
        or end.month not in (3, 6, 9, 12)
        or end.day != calendar.monthrange(end.year, end.month)[1]
    ):
        raise _ReadError("invalid_fact", "Fact has an invalid amount or measurement definition")
    if fact.metric_name == "total_debt":
        valid = (
            fact.value >= 0
            and fact.measurement_basis == "point_in_time"
            and fact.measurement_start is None
        )
    else:
        # Quarter-end anniversaries are valid dates in the previous year.
        expected_start = end.replace(year=end.year - 1) + timedelta(days=1)
        valid = fact.measurement_basis == "ttm" and fact.measurement_start == expected_start
    if not valid:
        raise _ReadError("invalid_fact", "Fact has an invalid measurement basis or window")


class CaseReadTools:
    def __init__(self, session: Session, context: CaseContext):
        self._session = session
        self._context = CaseContext.model_validate(context)

    def _authorize(self) -> tuple[ReportingPeriod, ReportingPeriod]:
        return authorize_credit_case(self._session, **self._context.model_dump())

    def _pages(self):
        scope = self._context
        return select(DocumentPage, Document).join(
            Document, Document.id == DocumentPage.document_id
        ).where(
            Document.tenant_id == scope.tenant_id,
            Document.borrower_id == scope.borrower_id,
            Document.reporting_period_id.in_((scope.prior_period_id, scope.current_period_id)),
        )

    @staticmethod
    def _check_page(page: DocumentPage) -> None:
        if not page.text_content.strip() or hashlib.sha256(page.text_content.encode()).hexdigest() != page.text_hash:
            raise _ReadError("invalid_evidence", "Source page is empty or its text hash does not match")

    def get_current_fact(self, arguments: dict[str, Any]) -> ToolResult[FactOutput]:
        return self._read_fact(arguments, period_index=1)

    def get_prior_fact(self, arguments: dict[str, Any]) -> ToolResult[FactOutput]:
        return self._read_fact(arguments, period_index=0)

    def _read_fact(
        self, arguments: dict[str, Any], *, period_index: Literal[0, 1]
    ) -> ToolResult[FactOutput]:
        try:
            request = FactRequest.model_validate(arguments)
        except ValidationError:
            return _failure("invalid_arguments", "Supply only one supported metric name")
        try:
            with self._session.no_autoflush:
                period = self._authorize()[period_index]
                fact = self._session.scalar(select(FinancialFact).where(
                    FinancialFact.borrower_id == self._context.borrower_id,
                    FinancialFact.reporting_period_id == period.id,
                    FinancialFact.metric_name == request.metric,
                    FinancialFact.is_canonical.is_(True),
                ))
                if fact is None:
                    return _failure("fact_not_found", "No canonical fact is available for this metric and period")
                _validate_fact(fact, period)
                row = self._session.execute(self._pages().where(
                    DocumentPage.id == fact.source_page_id
                )).one_or_none()
                if row is None:
                    return _failure("invalid_evidence", "Fact has no source page within the case scope")
                page, document = row
                self._check_page(page)
                if not fact.source_text or fact.source_text not in page.text_content:
                    return _failure("invalid_evidence", "Fact source quote is absent from its page")
                return ToolResult[FactOutput](ok=True, data=serialize_fact(fact, page, document))
        except (PermissionError, CreditDataError):
            return _failure("scope_unavailable", "Case scope is unavailable or invalid")
        except _ReadError as exc:
            return _failure(exc.code, str(exc))
        except DBAPIError:
            return _failure("data_unavailable", "The database read could not be completed")

    def get_document_page(self, arguments: dict[str, Any]) -> ToolResult[PageOutput]:
        try:
            request = PageRequest.model_validate(arguments)
        except ValidationError:
            return _failure("invalid_arguments", "Supply a document UUID and a positive integer page number")
        try:
            with self._session.no_autoflush:
                self._authorize()
                row = self._session.execute(self._pages().where(
                    Document.id == request.document_id,
                    DocumentPage.page_number == request.page_number,
                )).one_or_none()
                # Missing and out-of-scope documents have the same public error.
                if row is None:
                    return _failure("page_not_found", "Page is unavailable within this case")
                page, document = row
                self._check_page(page)
                return ToolResult[PageOutput](
                    ok=True, data=PageOutput(source=_page_reference(page, document), text=page.text_content)
                )
        except (PermissionError, CreditDataError):
            return _failure("scope_unavailable", "Case scope is unavailable or invalid")
        except _ReadError as exc:
            return _failure(exc.code, str(exc))
        except DBAPIError:
            return _failure("data_unavailable", "The database read could not be completed")
