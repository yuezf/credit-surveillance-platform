"""Run only against a disposable test DB alongside the existing integration suite."""

import os
import tempfile
import unittest
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, delete, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.credit_fixture import FixtureConflictError, seed_credit_fixture
from app.credit_service import CreditDataError, evaluate_credit_case
from app.models import Borrower, Document, DocumentChunk, DocumentPage, FinancialFact, ReportingPeriod, Tenant


@unittest.skipUnless(os.getenv("RUN_DATABASE_TESTS") == "1", "Set RUN_DATABASE_TESTS=1 for PostgreSQL fixture tests")
class CreditFixtureIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = os.environ["DATABASE_URL"]
        if not (make_url(url).database or "").endswith("_test"):
            raise RuntimeError("Fixture tests require a disposable database ending in _test")
        cls.engine = create_engine(url)
        cls.storage = tempfile.TemporaryDirectory()
        cls.root = Path(cls.storage.name)
        cls.models = (Tenant, Borrower, ReportingPeriod, Document, DocumentPage, FinancialFact)
        with Session(cls.engine, expire_on_commit=False) as session:
            before = {model: set(session.scalars(select(model.id))) for model in cls.models}
            cls.scope = seed_credit_fixture(session, storage_root=cls.root)
            cls.created = {model: set(session.scalars(select(model.id))) - before[model]
                           for model in cls.models}

    @classmethod
    def tearDownClass(cls):
        # Remove only rows created by this test class, retaining any API-test scope.
        with cls.engine.begin() as connection:
            for model in reversed(cls.models):
                if cls.created[model]:
                    connection.execute(delete(model).where(model.id.in_(cls.created[model])))
        cls.engine.dispose()
        cls.storage.cleanup()

    def setUp(self):
        self.session = Session(self.engine, expire_on_commit=False)
        self.args = dict(
            tenant_id=self.scope.tenant_id, borrower_id=self.scope.borrower_id,
            prior_period_id=self.scope.prior_period_id, current_period_id=self.scope.current_period_id,
        )

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def fact(self, metric="ebitda", period=None):
        return self.session.scalar(select(FinancialFact).where(
            FinancialFact.borrower_id == self.scope.borrower_id,
            FinancialFact.reporting_period_id == (period or self.scope.current_period_id),
            FinancialFact.metric_name == metric,
            FinancialFact.is_canonical.is_(True),
        ))

    def test_database_facts_produce_expected_leverage_and_critical_exception(self):
        result = evaluate_credit_case(self.session, **self.args)
        self.assertEqual(result.prior.calculation.ratio, Decimal("3.75"))
        self.assertFalse(result.prior.breached)
        self.assertEqual(result.current.calculation.ratio.quantize(Decimal("0.0001")), Decimal("4.8485"))
        self.assertEqual(result.current.severity, "critical")
        self.assertTrue(result.current.breached)
        self.assertFalse(self.session.new or self.session.dirty or self.session.deleted)

    def test_seed_twice_keeps_row_ids_timestamps_original_bytes_and_no_vectors(self):
        models = (Tenant, Borrower, ReportingPeriod, Document, DocumentPage, FinancialFact)
        before = {model: [(row.id, row.updated_at) for row in self.session.scalars(select(model).order_by(model.id))] for model in models}
        seed_credit_fixture(self.session, storage_root=self.root)
        seed_credit_fixture(self.session, storage_root=self.root)
        after = {model: [(row.id, row.updated_at) for row in self.session.scalars(select(model).order_by(model.id))] for model in models}
        self.assertEqual(before, after)
        facts = self.session.scalars(select(FinancialFact).where(FinancialFact.borrower_id == self.scope.borrower_id)).all()
        self.assertEqual(len(facts), 8)
        for fact in facts:
            page = self.session.get(DocumentPage, fact.source_page_id)
            self.assertIn(fact.source_text, page.text_content)
        document = self.session.scalar(select(Document).where(Document.borrower_id == self.scope.borrower_id))
        from app.credit_fixture import FIXTURE_PDF
        self.assertEqual(Path(document.storage_uri.removeprefix("file://")).read_bytes(), FIXTURE_PDF.read_bytes())
        self.assertEqual(document.status, "pending")
        self.assertEqual(self.session.scalar(select(func.count()).select_from(DocumentChunk).where(DocumentChunk.document_id == document.id)), 0)

    def test_conflict_rolls_back_without_overwriting_existing_value(self):
        fact_id = self.fact().id
        try:
            with self.engine.begin() as connection:
                connection.execute(update(FinancialFact).where(FinancialFact.id == fact_id).values(value=Decimal("999")))
            self.session.expire_all()
            with self.assertRaises(FixtureConflictError):
                seed_credit_fixture(self.session, storage_root=self.root)
            # The conflicting value was committed before seeding. It must survive.
            self.assertEqual(self.fact().value, Decimal("999"))
        finally:
            with self.engine.begin() as connection:
                connection.execute(update(FinancialFact).where(FinancialFact.id == fact_id).values(value=Decimal("330000000")))
            self.session.expire_all()

    def test_alternate_canonical_fact_is_an_explicit_conflict(self):
        original = self.fact()
        original.is_canonical = False
        self.session.flush()
        self.session.add(FinancialFact(
            borrower_id=original.borrower_id, reporting_period_id=original.reporting_period_id,
            metric_name="ebitda", value=Decimal("330000000"), unit="units", currency="USD",
            is_canonical=True,
        ))
        self.session.flush()
        with self.assertRaises(FixtureConflictError):
            seed_credit_fixture(self.session, storage_root=self.root)

    def test_storage_failure_rolls_back_new_database_content(self):
        # Delete only inside this uncommitted test transaction. Rollback restores
        # the baseline, including deletions, and removes the attempted rebuild.
        self.session.execute(delete(FinancialFact).where(FinancialFact.borrower_id == self.scope.borrower_id))
        self.session.execute(delete(Document).where(Document.borrower_id == self.scope.borrower_id))
        self.session.expire_all()
        with tempfile.TemporaryDirectory() as root:
            with patch("app.credit_fixture.LocalOriginalPDFStore.put_pdf", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    seed_credit_fixture(self.session, storage_root=Path(root))
        self.assertEqual(self.fact().value, Decimal("330000000"))
        self.assertEqual(self.session.scalar(select(func.count()).select_from(DocumentPage)), 2)

    def test_unauthorized_tenant_borrower_or_period_cannot_load_case(self):
        for field in ("tenant_id", "borrower_id", "prior_period_id", "current_period_id"):
            with self.subTest(field=field), self.assertRaises(PermissionError):
                evaluate_credit_case(self.session, **(self.args | {field: uuid.uuid4()}))

    def test_other_existing_tenant_cannot_read_demo_facts(self):
        other = Tenant(name="Other authenticated tenant")
        self.session.add(other)
        self.session.flush()
        with self.assertRaises(PermissionError):
            evaluate_credit_case(self.session, **(self.args | {"tenant_id": other.id}))

    def test_reversed_or_duplicate_periods_are_rejected(self):
        for overrides in (
            {"prior_period_id": self.scope.current_period_id, "current_period_id": self.scope.prior_period_id},
            {"prior_period_id": self.scope.current_period_id},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(CreditDataError):
                evaluate_credit_case(self.session, **(self.args | overrides))

    def test_missing_canonical_fact_is_uncalculable_not_a_pass(self):
        self.fact().is_canonical = False
        self.session.flush()
        result = evaluate_credit_case(self.session, **self.args)
        self.assertIsNone(result.current.breached)
        self.assertEqual(result.current.calculation.error_code, "missing_fact")

    def test_legacy_measurement_is_not_inferred(self):
        fact = self.fact()
        fact.measurement_basis = fact.measurement_start = fact.measurement_end = None
        self.session.flush()
        with self.assertRaises(CreditDataError):
            evaluate_credit_case(self.session, **self.args)

    def test_measurement_end_must_match_reporting_period(self):
        self.fact().measurement_end = date(2025, 3, 31)
        self.session.flush()
        with self.assertRaises(CreditDataError):
            evaluate_credit_case(self.session, **self.args)

    def test_non_ttm_window_cannot_produce_a_passing_result(self):
        self.fact().measurement_start = date(2025, 4, 1)
        self.session.flush()
        result = evaluate_credit_case(self.session, **self.args)
        self.assertIsNone(result.current.breached)
        self.assertEqual(result.current.calculation.error_code, "invalid_measurement_window")

    def test_missing_page_wrong_quote_and_modified_page_are_rejected(self):
        fact = self.fact()
        page = self.session.get(DocumentPage, fact.source_page_id)
        original_page_id, original_quote = fact.source_page_id, fact.source_text
        fact.source_page_id = None
        self.session.flush()
        with self.assertRaises(CreditDataError):
            evaluate_credit_case(self.session, **self.args)
        fact.source_page_id = original_page_id
        fact.source_text = "This quote does not exist."
        self.session.flush()
        with self.assertRaises(CreditDataError):
            evaluate_credit_case(self.session, **self.args)
        fact.source_text = original_quote
        page.text_content += " Modified."
        self.session.flush()
        with self.assertRaises(CreditDataError):
            evaluate_credit_case(self.session, **self.args)

    def test_source_document_must_stay_in_case_scope(self):
        page = self.session.get(DocumentPage, self.fact().source_page_id)
        document = self.session.get(Document, page.document_id)
        for field in ("tenant_id", "borrower_id", "reporting_period_id"):
            # DB foreign keys permit these individually valid but inconsistent
            # combinations. The service must reject each one.
            with self.subTest(field=field):
                original = getattr(document, field)
                if field == "tenant_id":
                    other = Tenant(name="Other evidence tenant")
                    self.session.add(other)
                    self.session.flush()
                    replacement = other.id
                elif field == "borrower_id":
                    other = Borrower(tenant_id=self.scope.tenant_id, legal_name="Other borrower")
                    self.session.add(other)
                    self.session.flush()
                    replacement = other.id
                else:
                    other = ReportingPeriod(
                        tenant_id=self.scope.tenant_id, borrower_id=self.scope.borrower_id,
                        period_type="quarter", period_start=date(2025, 7, 1),
                        period_end=date(2025, 9, 30), fiscal_year=2025, fiscal_quarter=3, label="Q3 2025",
                    )
                    self.session.add(other)
                    self.session.flush()
                    replacement = other.id
                setattr(document, field, replacement)
                self.session.flush()
                with self.assertRaises(CreditDataError):
                    evaluate_credit_case(self.session, **self.args)
                setattr(document, field, original)
                self.session.flush()

    def test_database_rejects_partial_measurement_metadata(self):
        fact = self.fact()
        with self.assertRaises(IntegrityError):
            with self.session.begin_nested():
                fact.measurement_basis = None
                self.session.flush()


if __name__ == "__main__":
    unittest.main()
