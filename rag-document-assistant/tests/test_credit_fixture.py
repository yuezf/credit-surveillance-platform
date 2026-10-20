import hashlib
import json
import unittest
import uuid
from datetime import date
from decimal import Decimal

from app.credit_fixture import FIXTURE_PDF, FIXTURE_ROOT, local_database_url
from app.credit_service import CreditDataError, fact_to_observation
from app.ingestion import extract_pages_from_pdf
from app.models import FinancialFact


class CreditFixtureUnitTests(unittest.TestCase):
    def fact(self, **changes):
        fields = dict(
            id=uuid.uuid4(), borrower_id=uuid.uuid4(), reporting_period_id=uuid.uuid4(),
            metric_name="ebitda", value=Decimal("330000000"), unit="units", currency="USD",
            is_canonical=True, measurement_basis="ttm",
            measurement_start=date(2024, 7, 1), measurement_end=date(2025, 6, 30),
        )
        fields.update(changes)
        return FinancialFact(**fields)

    def test_adapter_retains_identity_value_and_explicit_measurement_window(self):
        fact = self.fact()
        observation = fact_to_observation(fact)
        self.assertEqual(observation.fact_id, fact.id)
        self.assertEqual(observation.borrower_id, fact.borrower_id)
        self.assertEqual(observation.reporting_period_id, fact.reporting_period_id)
        self.assertEqual(observation.value, Decimal("330000000"))
        self.assertEqual(observation.period_start, date(2024, 7, 1))
        self.assertEqual(observation.period_end, date(2025, 6, 30))
        self.assertEqual((observation.basis, observation.scale), ("ttm", "units"))

    def test_adapter_rejects_legacy_or_unsupported_inputs(self):
        for changes in (
            {"measurement_basis": None}, {"measurement_end": None},
            {"unit": "USD"}, {"currency": None}, {"metric_name": "revenue"},
            {"is_canonical": False},
        ):
            with self.subTest(changes=changes), self.assertRaises(CreditDataError):
                fact_to_observation(self.fact(**changes))

    def test_pdf_hash_and_all_eight_source_quotes_match(self):
        manifest = json.loads((FIXTURE_ROOT / "example_corp_credit.json").read_text())
        self.assertEqual(hashlib.sha256(FIXTURE_PDF.read_bytes()).hexdigest(), manifest["pdf_sha256"])
        pages = extract_pages_from_pdf(str(FIXTURE_PDF))
        self.assertEqual([page.page_number for page in pages], [1, 2])
        self.assertEqual(sum(len(period["facts"]) for period in manifest["periods"]), 8)
        for period in manifest["periods"]:
            for fact in period["facts"]:
                self.assertIn(fact["source_text"], pages[period["page"] - 1].text)
        for explanation in manifest["explanation"]:
            self.assertIn(explanation, pages[1].text)

    def test_cli_accepts_loopback_only_and_rejects_host_overrides(self):
        local = "postgresql+psycopg://test:test@127.0.0.1:55432/rag_credit"
        self.assertEqual(local_database_url(local), local)
        for value in (
            "postgresql+psycopg://test:test@database.example.com/rag_credit",
            local + "?host=database.example.com", "sqlite:///local.db",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_database_url(value)


if __name__ == "__main__":
    unittest.main()
