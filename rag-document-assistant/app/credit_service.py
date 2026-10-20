"""Read authorized, canonical, page-supported facts and evaluate a credit case.

The caller supplies tenant identity from authentication, never from model output.
This service performs reads only; it neither seeds data nor persists exceptions.
"""

import hashlib
import uuid
from dataclasses import dataclass
from typing import cast, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.document_repository import credit_scope_exists
from app.leverage import FinancialObservation, LeverageRuleResult, evaluate_leverage_rule
from app.models import Document, DocumentPage, FinancialFact, ReportingPeriod


class CreditDataError(ValueError):
    """An existing canonical fact lacks valid measurement or page provenance."""


@dataclass(frozen=True)
class CreditCaseResult:
    tenant_id: uuid.UUID
    borrower_id: uuid.UUID
    prior_period_id: uuid.UUID
    current_period_id: uuid.UUID
    prior: LeverageRuleResult
    current: LeverageRuleResult


def fact_to_observation(fact: FinancialFact) -> FinancialObservation:
    """Map explicit database fields; never infer basis from the period label.

    This adapter does not authorize access or load evidence. Use it only after
    those checks, as evaluate_credit_case does below.
    """
    if not fact.is_canonical:
        raise CreditDataError("Only canonical facts can enter the calculation")
    if fact.metric_name not in ("total_debt", "ebitda"):
        raise CreditDataError("Unsupported leverage metric")
    if fact.unit not in ("units", "millions"):
        raise CreditDataError("Fact unit must explicitly specify units or millions")
    if fact.measurement_basis not in ("point_in_time", "ttm") or fact.measurement_end is None:
        raise CreditDataError("Fact has no explicit measurement definition")
    if fact.currency is None:
        raise CreditDataError("Fact has no explicit currency")
    return FinancialObservation(
        fact_id=fact.id,
        borrower_id=fact.borrower_id,
        reporting_period_id=fact.reporting_period_id,
        metric=cast(Literal["total_debt", "ebitda"], fact.metric_name),
        value=fact.value,
        currency=fact.currency,
        scale=cast(Literal["units", "millions"], fact.unit),
        basis=cast(Literal["point_in_time", "ttm"], fact.measurement_basis),
        period_start=fact.measurement_start,
        period_end=fact.measurement_end,
    )


def _load_period_observations(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    borrower_id: uuid.UUID,
    period: ReportingPeriod,
    allowed_periods: tuple[uuid.UUID, uuid.UUID],
) -> dict[str, FinancialObservation]:
    facts = session.scalars(
        select(FinancialFact).where(
            FinancialFact.borrower_id == borrower_id,
            FinancialFact.reporting_period_id == period.id,
            FinancialFact.metric_name.in_(("total_debt", "ebitda")),
            FinancialFact.is_canonical.is_(True),
        )
    ).all()
    observations = {}
    for fact in facts:
        page = session.scalar(
            select(DocumentPage)
            .join(Document, Document.id == DocumentPage.document_id)
            .where(
                DocumentPage.id == fact.source_page_id,
                Document.tenant_id == tenant_id,
                Document.borrower_id == borrower_id,
                Document.reporting_period_id.in_(allowed_periods),
            )
        )
        if (
            page is None
            or not fact.source_text
            or fact.source_text not in page.text_content
            or hashlib.sha256(page.text_content.encode()).hexdigest() != page.text_hash
        ):
            raise CreditDataError("Fact is missing intact, case-scoped source-page evidence")
        observation = fact_to_observation(fact)
        if observation.period_end != period.period_end:
            raise CreditDataError("Fact measurement end differs from its reporting period")
        observations[fact.metric_name] = observation
    return observations


def evaluate_credit_case(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    borrower_id: uuid.UUID,
    prior_period_id: uuid.UUID,
    current_period_id: uuid.UUID,
) -> CreditCaseResult:
    """Authorize BOTH periods before reading facts; fail closed on scope errors."""
    allowed_periods = (prior_period_id, current_period_id)
    for period_id in allowed_periods:
        if not credit_scope_exists(session, tenant_id, borrower_id, period_id):
            raise PermissionError("Case scope is unavailable to this tenant")
    prior = session.get(ReportingPeriod, prior_period_id)
    current = session.get(ReportingPeriod, current_period_id)
    assert prior is not None and current is not None
    if (
        prior.period_type != "quarter"
        or current.period_type != "quarter"
        or prior.period_end >= current.period_end
    ):
        raise CreditDataError("Case requires two chronologically ordered quarter periods")
    results = []
    for period in (prior, current):
        observations = _load_period_observations(
            session, tenant_id=tenant_id, borrower_id=borrower_id,
            period=period, allowed_periods=allowed_periods,
        )
        results.append(evaluate_leverage_rule(
            observations.get("total_debt"), observations.get("ebitda")
        ))
    return CreditCaseResult(
        tenant_id, borrower_id, prior_period_id, current_period_id, results[0], results[1]
    )
