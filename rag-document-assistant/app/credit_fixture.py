"""Local-only, idempotent synthetic case. No LLM, embeddings, or AWS clients."""

import argparse
import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.credit_service import CreditCaseResult, evaluate_credit_case
from app.demo_seed import DemoScope, seed_demo_scope
from app.document_service import build_document_id, build_page_id
from app.ingestion import extract_pages_from_pdf
from app.models import Borrower, Document, DocumentPage, FinancialFact, ReportingPeriod, Tenant
from app.original_storage import LocalOriginalPDFStore, pdf_object_key


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
FIXTURE_PDF = FIXTURE_ROOT / "example_corp_credit.pdf"


class FixtureConflictError(ValueError):
    """Existing data differs from the fixture; no database changes are committed."""


def _ensure_row(session: Session, model, row_id: uuid.UUID, **fields):
    """Helper function that creates or verifies a row, making repeat runs idempotent.
    They preserve the same intended state instead of adding duplicates or overwriting differences.
    Row absent--Insert it
    Row exists and matches--Reuse it
    Row exists but differ--Raise an error
    """
    existing = session.get(model, row_id)
    if existing is None:
        existing = model(id=row_id, **fields)
        session.add(existing)
        session.flush()
    else:
        for field, expected in fields.items():
            if getattr(existing, field) != expected:
                raise FixtureConflictError(f"{model.__name__} {row_id}: conflicting {field}")
    return existing


def _verify_demo_scope(session: Session, scope: DemoScope) -> None:
    # seed_demo_scope retains its old create-if-absent contract. Verify that
    # pre-existing stable IDs really identify the intended fixture entities.
    _ensure_row(session, Tenant, scope.tenant_id, name="Demo Tenant")
    _ensure_row(
        session, Borrower, scope.borrower_id, tenant_id=scope.tenant_id,
        legal_name="Example Corp", external_id="example-corp", status="active",
    )
    for period_id, start, end, quarter in (
        (scope.prior_period_id, date(2025, 1, 1), date(2025, 3, 31), 1),
        (scope.current_period_id, date(2025, 4, 1), date(2025, 6, 30), 2),
    ):
        _ensure_row(
            session, ReportingPeriod, period_id, tenant_id=scope.tenant_id,
            borrower_id=scope.borrower_id, period_type="quarter", period_start=start,
            period_end=end, fiscal_year=2025, fiscal_quarter=quarter, label=f"Q{quarter} 2025",
        )


def seed_credit_fixture(session: Session, *, storage_root: Path) -> DemoScope:
    """Own one DB transaction; exact matches are no-ops, conflicts fail closed.

    Original storage is explicitly LOCAL, independent of environment settings.
    PDF bytes and database transactions cannot commit atomically; a DB commit
    failure can leave a harmless content-addressed local PDF for the next retry.
    No fake embeddings are inserted. Document indexing stays pending.
    """
    manifest = json.loads((FIXTURE_ROOT / "example_corp_credit.json").read_text())
    content = FIXTURE_PDF.read_bytes()
    content_hash = hashlib.sha256(content).hexdigest()
    if content_hash != manifest["pdf_sha256"]:
        raise FixtureConflictError("Fixture PDF hash differs from its manifest")
    pages = extract_pages_from_pdf(str(FIXTURE_PDF))
    if [page.page_number for page in pages] != [1, 2]:
        raise FixtureConflictError("Fixture requires exactly two nonempty pages")
    store = LocalOriginalPDFStore(storage_root)
    try:
        scope = seed_demo_scope(session, commit=False)
        _verify_demo_scope(session, scope)
        document_id = build_document_id(scope.tenant_id, content_hash)
        destination = store.root / pdf_object_key(scope.tenant_id, content_hash)
        if destination.exists() and destination.read_bytes() != content:
            raise FixtureConflictError("Existing local original has different bytes")
        _ensure_row(
            session, Document, document_id, tenant_id=scope.tenant_id,
            borrower_id=scope.borrower_id, reporting_period_id=scope.current_period_id,
            original_filename=FIXTURE_PDF.name, content_hash=content_hash,
            media_type="application/pdf", document_type="supporting", byte_size=len(content),
            storage_uri=destination.as_uri(), status="pending",
        )
        for page in pages:
            _ensure_row(
                session, DocumentPage, build_page_id(document_id, page.page_number),
                document_id=document_id, page_number=page.page_number,
                text_content=page.text, text_hash=hashlib.sha256(page.text.encode()).hexdigest(),
                extraction_method="pypdf",
            )
        for period_spec, period_id in zip(
            manifest["periods"], (scope.prior_period_id, scope.current_period_id), strict=True
        ):
            page = pages[period_spec["page"] - 1]
            for spec in period_spec["facts"]:
                if spec["source_text"] not in page.text:
                    raise FixtureConflictError("Fact source text is absent from the PDF page")
                fact_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"rag-credit:fixture:v1:fact:{scope.borrower_id}:{period_id}:{spec['metric']}",
                )
                canonical = session.scalar(select(FinancialFact).where(
                    FinancialFact.borrower_id == scope.borrower_id,
                    FinancialFact.reporting_period_id == period_id,
                    FinancialFact.metric_name == spec["metric"],
                    FinancialFact.is_canonical.is_(True),
                ))
                if canonical is not None and canonical.id != fact_id:
                    raise FixtureConflictError("A different canonical fact already exists")
                _ensure_row(
                    session, FinancialFact, fact_id, borrower_id=scope.borrower_id,
                    reporting_period_id=period_id,
                    source_page_id=build_page_id(document_id, page.page_number),
                    metric_name=spec["metric"], value=Decimal(spec["value"]),
                    unit="units", currency="USD", source_text=spec["source_text"],
                    confidence=None, is_canonical=True, measurement_basis=spec["basis"],
                    measurement_start=(date.fromisoformat(spec["measurement_start"])
                                       if spec["measurement_start"] else None),
                    measurement_end=date.fromisoformat(spec["measurement_end"]),
                )
        # Validate the complete case before committing any rows or writing bytes.
        evaluate_credit_case(
            session, tenant_id=scope.tenant_id, borrower_id=scope.borrower_id,
            prior_period_id=scope.prior_period_id, current_period_id=scope.current_period_id,
        )
        if not destination.exists():
            store.put_pdf(FIXTURE_PDF, scope.tenant_id, content_hash)
        session.commit()
        return scope
    except IntegrityError as exc:
        session.rollback()
        raise FixtureConflictError("Fixture conflicts with an existing unique record") from exc
    except Exception:
        session.rollback()
        raise


def local_database_url(value: str) -> str:
    """Refuse remote destinations and query-string host overrides for this CLI."""
    url = make_url(value)
    if (
        url.drivername != "postgresql+psycopg"
        or url.host not in ("localhost", "127.0.0.1", "::1")
        or url.query
    ):
        raise ValueError("Fixture CLI requires an explicit loopback PostgreSQL URL without query parameters")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, type=local_database_url)
    parser.add_argument(
        "--storage-root", type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "originals",
    )
    args = parser.parse_args()
    engine = create_engine(args.database_url)
    try:
        with Session(engine, expire_on_commit=False) as session:
            scope = seed_credit_fixture(session, storage_root=args.storage_root)
            result: CreditCaseResult = evaluate_credit_case(
                session, tenant_id=scope.tenant_id, borrower_id=scope.borrower_id,
                prior_period_id=scope.prior_period_id, current_period_id=scope.current_period_id,
            )
            for label, rule in (("Q1 2025", result.prior), ("Q2 2025", result.current)):
                print(f"{label}: {rule.calculation.ratio:.4f}x - {rule.severity or 'pass'}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
