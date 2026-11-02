import hashlib
import os
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.case_tools import CaseContext
from app.credit_fixture import local_database_url, seed_credit_fixture
from app.credit_indexing import EmbeddingError, IndexConflictError, index_case_document
from app.investigation_tools import InvestigationTools
from app.models import Borrower, Document, DocumentChunk, DocumentPage, FinancialFact, ReportingPeriod, Tenant
from tests.investigation_support import FixtureEmbeddings


@unittest.skipUnless(os.getenv("RUN_DATABASE_TESTS") == "1", "Set RUN_DATABASE_TESTS=1 for investigation tests")
class InvestigationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = local_database_url(os.environ["DATABASE_URL"])
        if not (make_url(url).database or "").endswith("_test"):
            raise RuntimeError("Investigation tests require a disposable database ending in _test")
        cls.engine = create_engine(url)
        cls.storage = tempfile.TemporaryDirectory()
        cls.root = Path(cls.storage.name)
        cls.models = (Tenant, Borrower, ReportingPeriod, Document, DocumentPage, FinancialFact)
        with Session(cls.engine, expire_on_commit=False) as session:
            before = {model: set(session.scalars(select(model.id))) for model in cls.models}
            scope = seed_credit_fixture(session, storage_root=cls.root)
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
        self.backend = FixtureEmbeddings()
        self.tools = InvestigationTools(self.session, self.context, embeddings=self.backend)
        self.document = self.session.scalar(select(Document).where(Document.borrower_id == self.context.borrower_id))

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def index(self, backend=None):
        return index_case_document(self.session, self.context, self.document.id, backend or self.backend)

    def page_args(self):
        return {"document_id": str(self.document.id), "page_number": 2}

    def finding_args(self, **citation_changes):
        citation = self.page_args() | {"quote": "Lower customer volumes: USD 45 million reduction in EBITDA."}
        return {"claims": [{
            "text": "Lower customer volumes contributed to the decline in TTM EBITDA.",
            "citations": [citation | citation_changes],
        }]}

    def test_index_preserves_pages_and_fact_links_and_reuses_without_embedding_calls(self):
        pages_before = [(p.id, p.text_hash) for p in self.session.scalars(select(DocumentPage).order_by(DocumentPage.id))]
        facts_before = [(f.id, f.source_page_id) for f in self.session.scalars(select(FinancialFact).order_by(FinancialFact.id))]
        first = self.index()
        second = self.index()
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(first.chunk_count, second.chunk_count)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.document.status, "completed")
        self.assertEqual(pages_before, [(p.id, p.text_hash) for p in self.session.scalars(select(DocumentPage).order_by(DocumentPage.id))])
        self.assertEqual(facts_before, [(f.id, f.source_page_id) for f in self.session.scalars(select(FinancialFact).order_by(FinancialFact.id))])

    def test_failed_embeddings_leave_no_index_or_status_change(self):
        backend = MagicMock(model_name="broken-model")
        backend.embed.side_effect = RuntimeError("private error")
        with self.assertRaises(EmbeddingError):
            self.index(backend)
        self.session.rollback()
        self.assertEqual(self.session.get(Document, self.document.id).status, "pending")
        self.assertEqual(self.session.scalars(select(DocumentChunk)).all(), [])

    def test_index_conflict_does_not_replace_existing_model_or_chunks(self):
        self.index()
        ids = list(self.session.scalars(select(DocumentChunk.id)))
        other = MagicMock(model_name="different-model")
        with self.assertRaises(IndexConflictError):
            self.index(other)
        other.embed.assert_not_called()
        self.assertEqual(ids, list(self.session.scalars(select(DocumentChunk.id))))

    def test_unauthorized_index_is_denied_before_embedding(self):
        context = CaseContext(**(self.context.model_dump() | {"tenant_id": uuid.uuid4()}))
        with self.assertRaises(PermissionError):
            index_case_document(self.session, context, self.document.id, self.backend)
        self.assertEqual(self.backend.calls, [])

    def test_modified_page_cannot_be_indexed(self):
        page = self.session.scalar(select(DocumentPage).where(DocumentPage.page_number == 2))
        page.text_content += " modified"
        self.session.flush()
        with self.assertRaises(IndexConflictError):
            self.index()
        self.assertEqual(self.backend.calls, [])

    def test_reseeding_after_indexing_preserves_the_index_and_fact_links(self):
        self.index()
        self.session.commit()
        chunk_ids = list(self.session.scalars(select(DocumentChunk.id).order_by(DocumentChunk.id)))
        fact_links = [(f.id, f.source_page_id) for f in self.session.scalars(select(FinancialFact).order_by(FinancialFact.id))]
        try:
            seed_credit_fixture(self.session, storage_root=self.root)
            self.assertEqual(self.session.get(Document, self.document.id).status, "completed")
            self.assertEqual(chunk_ids, list(self.session.scalars(select(DocumentChunk.id).order_by(DocumentChunk.id))))
            self.assertEqual(fact_links, [(f.id, f.source_page_id) for f in self.session.scalars(select(FinancialFact).order_by(FinancialFact.id))])
        finally:
            self.session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == self.document.id))
            self.session.get(Document, self.document.id).status = "pending"
            self.session.commit()

    def test_search_then_exact_page_then_draft_finding_closes_the_investigation(self):
        self.index()
        search = self.tools.search_documents({"query": "Why did EBITDA decline?", "top_k": 1})
        self.assertTrue(search.ok, search.error)
        self.assertEqual(len(search.data.matches), 1)
        self.assertEqual(search.data.matches[0].sources[0].page_number, 2)
        self.assertTrue(self.tools.get_document_page(self.page_args()).ok)
        result = self.tools.create_finding(self.finding_args())
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data.status, "draft_finding")
        self.assertTrue(result.data.requires_human_review)
        self.assertEqual(result.data.rule.prior_leverage, "3.75")
        self.assertEqual(result.data.rule.severity, "critical")
        self.assertEqual(result.data.claims[0].citations[0].source.page_number, 2)
        for closed in (
            self.tools.get_current_fact({"metric": "ebitda"}),
            self.tools.get_prior_fact({"metric": "ebitda"}),
            self.tools.get_document_page(self.page_args()),
            self.tools.search_documents({"query": "EBITDA"}),
            self.tools.create_finding(self.finding_args()),
            self.tools.request_human_review({"reason": "Retry"}),
        ):
            self.assertEqual(closed.error.code, "already_terminated")
        self.assertEqual(self.tools.outcome, result.data)

    def test_unindexed_case_and_wrong_embedding_model_return_explicit_errors_without_calls(self):
        self.assertEqual(self.tools.search_documents({"query": "EBITDA"}).error.code, "index_unavailable")
        self.assertEqual(self.backend.calls, [])
        self.index()
        other = MagicMock(model_name="different-model")
        tools = InvestigationTools(self.session, self.context, embeddings=other)
        self.assertEqual(tools.search_documents({"query": "EBITDA"}).error.code, "index_unavailable")
        other.embed.assert_not_called()

    def test_no_backend_returns_explicit_error(self):
        tools = InvestigationTools(self.session, self.context)
        self.assertEqual(tools.search_documents({"query": "EBITDA"}).error.code, "embedding_unavailable")

    def test_search_embedding_failure_is_explicit_and_does_not_terminate(self):
        self.index()
        backend = MagicMock(model_name=self.backend.model_name)
        backend.embed.side_effect = RuntimeError("private provider credentials")
        tools = InvestigationTools(self.session, self.context, embeddings=backend)
        result = tools.search_documents({"query": "EBITDA"})
        self.assertEqual(result.error.code, "embedding_unavailable")
        self.assertNotIn("private", result.model_dump_json())
        self.assertIsNone(tools.outcome)

    def test_irrelevant_query_returns_no_matches_then_can_escalate(self):
        self.index()
        result = self.tools.search_documents({"query": "unrelated astronomy volcano"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data.matches, ())
        review = self.tools.request_human_review({
            "reason": "No relevant supporting evidence was found.",
            "unresolved_questions": ["What caused the EBITDA decline?"],
        })
        self.assertTrue(review.ok)
        self.assertEqual(review.data.status, "human_review")
        self.assertEqual(self.tools.request_human_review({"reason": "again"}).error.code, "already_terminated")

    def test_search_and_fact_results_do_not_count_as_full_page_inspection(self):
        self.index()
        self.tools.search_documents({"query": "EBITDA decline"})
        self.tools.get_current_fact({"metric": "ebitda"})
        result = self.tools.create_finding(self.finding_args())
        self.assertEqual(result.error.code, "evidence_not_inspected")
        self.assertIsNone(self.tools.outcome)
        self.tools.get_document_page(self.page_args())
        self.assertTrue(self.tools.create_finding(self.finding_args()).ok)

    def test_absent_quote_wrong_page_and_out_of_case_citation_are_rejected(self):
        self.tools.get_document_page(self.page_args())
        for changes in (
            {"quote": "This quote is fabricated."}, {"page_number": 99},
            {"document_id": str(uuid.uuid4())},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.tools.create_finding(self.finding_args(**changes)).error.code, "invalid_citation")
                self.assertIsNone(self.tools.outcome)

    def test_inspection_state_does_not_transfer_to_another_investigation(self):
        self.tools.get_document_page(self.page_args())
        other = InvestigationTools(self.session, self.context)
        self.assertEqual(other.create_finding(self.finding_args()).error.code, "evidence_not_inspected")

    def test_changed_page_after_inspection_must_be_fetched_again(self):
        page = self.session.scalar(select(DocumentPage).where(DocumentPage.page_number == 2))
        original_text, original_hash = page.text_content, page.text_hash
        self.tools.get_document_page(self.page_args())
        new_text = original_text + "\nAdditional synthetic note."
        try:
            with self.engine.begin() as connection:
                connection.execute(update(DocumentPage).where(DocumentPage.id == page.id).values(
                    text_content=new_text, text_hash=hashlib.sha256(new_text.encode()).hexdigest(),
                ))
            self.assertEqual(self.tools.create_finding(self.finding_args()).error.code, "invalid_citation")
            self.assertTrue(self.tools.get_document_page(self.page_args()).ok)
            self.assertTrue(self.tools.create_finding(self.finding_args()).ok)
        finally:
            with self.engine.begin() as connection:
                connection.execute(update(DocumentPage).where(DocumentPage.id == page.id).values(
                    text_content=original_text, text_hash=original_hash,
                ))

    def test_missing_fact_prevents_finding_but_allows_review(self):
        self.tools.get_document_page(self.page_args())
        self.session.scalar(select(FinancialFact).where(
            FinancialFact.reporting_period_id == self.context.current_period_id,
            FinancialFact.metric_name == "ebitda",
        )).is_canonical = False
        self.session.flush()
        self.assertEqual(self.tools.create_finding(self.finding_args()).error.code, "case_not_evaluable")
        self.assertTrue(self.tools.request_human_review({"reason": "Current EBITDA is unavailable."}).ok)

    def test_rule_decision_is_computed_by_server_and_cannot_be_invented(self):
        self.tools.get_document_page(self.page_args())
        self.session.scalar(select(FinancialFact).where(
            FinancialFact.reporting_period_id == self.context.current_period_id,
            FinancialFact.metric_name == "total_debt",
        )).value = Decimal("1000000000")
        self.session.flush()
        self.assertEqual(self.tools.create_finding(self.finding_args()).error.code, "no_critical_exception")
        self.assertIsNone(self.tools.outcome)

    def test_unauthorized_scope_blocks_new_tools_before_embedding_or_terminal_state(self):
        context = CaseContext(**(self.context.model_dump() | {"tenant_id": uuid.uuid4()}))
        tools = InvestigationTools(self.session, context, embeddings=self.backend)
        for result in (
            tools.search_documents({"query": "EBITDA"}),
            tools.create_finding(self.finding_args()),
            tools.request_human_review({"reason": "Missing evidence"}),
        ):
            self.assertEqual(result.error.code, "scope_unavailable")
        self.assertEqual(self.backend.calls, [])
        self.assertIsNone(tools.outcome)

    def test_search_cannot_widen_to_other_document_scopes(self):
        self.index()
        other = Borrower(tenant_id=self.context.tenant_id, legal_name="Other borrower")
        self.session.add(other)
        self.session.flush()
        self.document.borrower_id = other.id
        self.session.flush()
        self.assertEqual(self.tools.search_documents({"query": "EBITDA decline"}).error.code, "index_unavailable")

    def test_search_rejects_text_that_no_longer_matches_pages(self):
        self.index()
        chunk = self.session.scalar(select(DocumentChunk).where(DocumentChunk.start_page == 2))
        chunk.text_content = "Invented evidence"
        self.session.flush()
        self.assertEqual(self.tools.search_documents({"query": "EBITDA decline"}).error.code, "invalid_evidence")


if __name__ == "__main__":
    unittest.main()
