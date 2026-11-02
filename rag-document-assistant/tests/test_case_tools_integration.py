"""Exercise real scope filtering and provenance against a disposable local DB."""

import os
import tempfile
import unittest
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.case_tools import CaseContext, CaseReadTools
from app.credit_fixture import local_database_url, seed_credit_fixture
from app.models import Borrower, Document, DocumentPage, FinancialFact, ReportingPeriod, Tenant


@unittest.skipUnless(os.getenv("RUN_DATABASE_TESTS") == "1", "Set RUN_DATABASE_TESTS=1 for case-tool tests")
class CaseToolsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = local_database_url(os.environ["DATABASE_URL"])
        if not (make_url(url).database or "").endswith("_test"):
            raise RuntimeError("Case-tool tests require a disposable database ending in _test")
        cls.engine = create_engine(url)
        cls.storage = tempfile.TemporaryDirectory()
        cls.models = (Tenant, Borrower, ReportingPeriod, Document, DocumentPage, FinancialFact)
        with Session(cls.engine, expire_on_commit=False) as session:
            before = {model: set(session.scalars(select(model.id))) for model in cls.models}
            scope = seed_credit_fixture(session, storage_root=Path(cls.storage.name))
            cls.context = CaseContext(**scope.__dict__)
            cls.created = {model: set(session.scalars(select(model.id))) - before[model] for model in cls.models}

    @classmethod
    def tearDownClass(cls):
        with cls.engine.begin() as connection:
            for model in reversed(cls.models):
                if cls.created[model]:
                    connection.execute(delete(model).where(model.id.in_(cls.created[model])))
        cls.engine.dispose()
        cls.storage.cleanup()

    def setUp(self):
        self.session = Session(self.engine, expire_on_commit=False)
        self.tools = CaseReadTools(self.session, self.context)
        self.document = self.session.scalar(select(Document).where(Document.borrower_id == self.context.borrower_id))

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def fact(self, metric="ebitda", period_id=None):
        return self.session.scalar(select(FinancialFact).where(
            FinancialFact.borrower_id == self.context.borrower_id,
            FinancialFact.reporting_period_id == (period_id or self.context.current_period_id),
            FinancialFact.metric_name == metric,
            FinancialFact.is_canonical.is_(True),
        ))

    def page_args(self, **overrides):
        return {"document_id": str(self.document.id), "page_number": 2} | overrides

    def test_reads_all_four_metrics_from_both_periods_as_decimal_strings(self):
        expected = {
            "revenue": ("2400000000", "2200000000"),
            "total_debt": ("1500000000", "1600000000"),
            "ebitda": ("400000000", "330000000"),
            "interest_expense": ("80000000", "95000000"),
        }
        for metric, values in expected.items():
            for index, method in enumerate((self.tools.get_prior_fact, self.tools.get_current_fact)):
                with self.subTest(metric=metric, period=index):
                    result = method({"metric": metric})
                    self.assertTrue(result.ok, result.error)
                    payload = result.model_dump(mode="json")["data"]
                    self.assertIsInstance(payload["value"], str)
                    self.assertEqual(Decimal(payload["value"]), Decimal(values[index]))
                    self.assertEqual(payload["source"]["page_number"], index + 1)
                    self.assertEqual(payload["measurement_end"], ("2025-03-31", "2025-06-30")[index])
                    self.assertEqual(payload["basis"], "point_in_time" if metric == "total_debt" else "ttm")

    def test_fact_evidence_can_be_followed_to_its_exact_page(self):
        fact = self.tools.get_prior_fact({"metric": "ebitda"}).data
        result = self.tools.get_document_page({
            "document_id": str(fact.source.document_id), "page_number": fact.source.page_number,
        })
        self.assertTrue(result.ok)
        self.assertIn(fact.source_text, result.data.text)
        self.assertEqual(result.data.source.page_id, fact.source.page_id)
        # A prior-period fact legitimately cites a comparative current-period document.
        self.assertEqual(fact.reporting_period_id, self.context.prior_period_id)
        self.assertEqual(fact.source.reporting_period_id, self.context.current_period_id)

    def test_pending_document_pages_are_readable_without_embeddings(self):
        self.assertEqual(self.document.status, "pending")
        self.assertTrue(self.tools.get_document_page(self.page_args()).ok)

    def test_only_canonical_facts_are_returned(self):
        original = self.fact()
        self.session.add(FinancialFact(
            borrower_id=original.borrower_id, reporting_period_id=original.reporting_period_id,
            metric_name="ebitda", value=Decimal("999"), unit="units", currency="USD", is_canonical=False,
        ))
        self.session.flush()
        result = self.tools.get_current_fact({"metric": "ebitda"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data.fact_id, original.id)

    def test_missing_canonical_fact_returns_structured_error(self):
        self.fact().is_canonical = False
        self.session.flush()
        result = self.tools.get_current_fact({"metric": "ebitda"})
        self.assertFalse(result.ok)
        self.assertIsNone(result.data)
        self.assertEqual(result.error.code, "fact_not_found")

    def test_missing_page_and_unknown_document_have_same_public_error(self):
        missing = self.tools.get_document_page(self.page_args(page_number=99))
        unknown = self.tools.get_document_page(self.page_args(document_id=str(uuid.uuid4())))
        self.assertEqual(missing.error, unknown.error)
        self.assertEqual(missing.error.code, "page_not_found")

    def test_wrong_tenant_borrower_and_both_periods_deny_all_three_tools(self):
        other = Tenant(name="Other authenticated tenant")
        self.session.add(other)
        self.session.flush()
        for field in ("tenant_id", "borrower_id", "prior_period_id", "current_period_id"):
            values = self.context.model_dump() | {field: other.id if field == "tenant_id" else uuid.uuid4()}
            tools = CaseReadTools(self.session, CaseContext(**values))
            for result in (
                tools.get_prior_fact({"metric": "ebitda"}),
                tools.get_current_fact({"metric": "ebitda"}),
                tools.get_document_page(self.page_args()),
            ):
                with self.subTest(field=field):
                    self.assertEqual(result.error.code, "scope_unavailable")

    def test_reversed_and_duplicate_case_periods_are_rejected(self):
        for overrides in (
            {"prior_period_id": self.context.current_period_id},
            {"prior_period_id": self.context.current_period_id, "current_period_id": self.context.prior_period_id},
        ):
            tools = CaseReadTools(self.session, CaseContext(**(self.context.model_dump() | overrides)))
            self.assertEqual(tools.get_current_fact({"metric": "ebitda"}).error.code, "scope_unavailable")

    def test_ownership_is_rechecked_after_a_previous_success(self):
        self.assertTrue(self.tools.get_current_fact({"metric": "ebitda"}).ok)
        other = Tenant(name="New owner")
        self.session.add(other)
        self.session.flush()
        self.session.get(Borrower, self.context.borrower_id).tenant_id = other.id
        self.session.flush()
        self.assertEqual(self.tools.get_current_fact({"metric": "ebitda"}).error.code, "scope_unavailable")
        self.assertEqual(self.tools.get_document_page(self.page_args()).error.code, "scope_unavailable")

    def test_documents_from_other_tenants_borrowers_and_periods_are_unavailable(self):
        original = self.document
        references = []
        other_tenant = Tenant(name="Other evidence tenant")
        self.session.add(other_tenant)
        self.session.flush()
        references.append(("tenant_id", other_tenant.id))
        other_borrower = Borrower(tenant_id=self.context.tenant_id, legal_name="Other borrower")
        self.session.add(other_borrower)
        self.session.flush()
        references.append(("borrower_id", other_borrower.id))
        other_period = ReportingPeriod(
            tenant_id=self.context.tenant_id, borrower_id=self.context.borrower_id,
            period_type="quarter", period_start=date(2025, 7, 1), period_end=date(2025, 9, 30),
            fiscal_year=2025, fiscal_quarter=3, label="Q3 2025",
        )
        self.session.add(other_period)
        self.session.flush()
        references.append(("reporting_period_id", other_period.id))
        for field, replacement in references:
            with self.subTest(field=field):
                previous = getattr(original, field)
                setattr(original, field, replacement)
                self.session.flush()
                result = self.tools.get_document_page(self.page_args())
                self.assertEqual(result.error.code, "page_not_found")
                fact = self.tools.get_current_fact({"metric": "ebitda"})
                self.assertEqual(fact.error.code, "invalid_evidence")
                setattr(original, field, previous)
                self.session.flush()

    def test_modified_page_text_is_not_returned(self):
        page = self.session.get(DocumentPage, self.fact().source_page_id)
        page.text_content += " unexpected modification"
        self.session.flush()
        for result in (
            self.tools.get_document_page(self.page_args()),
            self.tools.get_current_fact({"metric": "ebitda"}),
        ):
            self.assertEqual(result.error.code, "invalid_evidence")
            self.assertIsNone(result.data)

    def test_wrong_quote_or_missing_page_reference_is_rejected(self):
        fact = self.fact()
        fact.source_text = "Not on the page"
        self.session.flush()
        self.assertEqual(self.tools.get_current_fact({"metric": "ebitda"}).error.code, "invalid_evidence")
        fact.source_page_id = None
        self.session.flush()
        self.assertEqual(self.tools.get_current_fact({"metric": "ebitda"}).error.code, "invalid_evidence")

    def test_legacy_measurement_fields_are_not_guessed(self):
        fact = self.fact()
        fact.measurement_basis = fact.measurement_start = fact.measurement_end = None
        self.session.flush()
        self.assertEqual(self.tools.get_current_fact({"metric": "ebitda"}).error.code, "invalid_fact")

    def test_wrong_window_and_scale_are_rejected(self):
        fact = self.fact()
        fact.measurement_start = date(2025, 4, 1)
        self.session.flush()
        self.assertEqual(self.tools.get_current_fact({"metric": "ebitda"}).error.code, "invalid_fact")
        fact.measurement_start = date(2024, 7, 1)
        fact.unit = "USD"
        self.session.flush()
        self.assertEqual(self.tools.get_current_fact({"metric": "ebitda"}).error.code, "invalid_fact")

    def test_nonpositive_ebitda_is_readable_without_making_a_rule_decision(self):
        # These tools report authoritative stored values; the calculator handles
        # the denominator rule. A quote match does not independently validate amount.
        self.fact().value = Decimal("-1")
        self.session.flush()
        result = self.tools.get_current_fact({"metric": "ebitda"})
        self.assertTrue(result.ok)
        self.assertEqual(Decimal(result.data.value), Decimal("-1"))

    def test_reads_never_autoflush_or_commit_unrelated_pending_writes(self):
        marker = Tenant(name="Pending caller write")
        self.session.add(marker)
        for result in (
            self.tools.get_prior_fact({"metric": "ebitda"}),
            self.tools.get_current_fact({"metric": "ebitda"}),
            self.tools.get_document_page(self.page_args()),
        ):
            self.assertTrue(result.ok, result.error)
        self.assertIn(marker, self.session.new)
        with self.engine.connect() as connection:
            self.assertIsNone(connection.scalar(select(Tenant.id).where(Tenant.name == marker.name)))


if __name__ == "__main__":
    unittest.main()
