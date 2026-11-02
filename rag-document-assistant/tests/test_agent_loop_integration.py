import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.agent_loop import run_investigation
from app.case_tools import CaseContext
from app.credit_fixture import local_database_url, seed_credit_fixture
from app.credit_indexing import index_case_document
from app.models import Document, Tenant
from tests.investigation_support import FixtureEmbeddings
from tests.test_agent_loop import FakeModel, turn


@unittest.skipUnless(os.getenv("RUN_DATABASE_TESTS") == "1", "Set RUN_DATABASE_TESTS=1 for agent tests")
class AgentLoopIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = local_database_url(os.environ["DATABASE_URL"])
        if not (make_url(url).database or "").endswith("_test"):
            raise RuntimeError("Agent tests require a disposable local _test database")
        cls.engine = create_engine(url)
        cls.storage = tempfile.TemporaryDirectory()
        with Session(cls.engine) as session:
            # This suite owns a fresh test fixture, never a user's demo case.
            if session.scalar(select(Tenant.id)) is not None:
                raise RuntimeError("Agent integration suite requires an empty database")
            scope = seed_credit_fixture(session, storage_root=Path(cls.storage.name))
            cls.context = CaseContext(**scope.__dict__)

    @classmethod
    def tearDownClass(cls):
        with cls.engine.begin() as connection:
            connection.execute(delete(Tenant).where(Tenant.id == cls.context.tenant_id))
        cls.engine.dispose()
        cls.storage.cleanup()

    def setUp(self):
        self.session = Session(self.engine)
        self.backend = FixtureEmbeddings()
        self.document_id = self.session.scalar(select(Document.id))
        self.page_args = {"document_id": str(self.document_id), "page_number": 2}

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def run_model(self, model, **kwargs):
        return run_investigation(self.session, self.context, model, embeddings=self.backend, **kwargs)

    def test_successful_investigation_reads_facts_searches_inspects_then_drafts(self):
        index_case_document(self.session, self.context, self.document_id, self.backend)
        calls = [turn(name, {"metric": metric}, f"c{i}") for i, (name, metric) in enumerate((
            ("get_prior_fact", "total_debt"), ("get_current_fact", "total_debt"),
            ("get_prior_fact", "ebitda"), ("get_current_fact", "ebitda"),
        ))]
        calls += [turn("search_documents", {"query": "Why did EBITDA decline?"}, "search"),
            turn("get_document_page", self.page_args, "page"),
            turn("create_finding", {"claims": [{
                "text": "Lower volumes contributed to the EBITDA decline.",
                "citations": [self.page_args | {"quote": "Lower customer volumes: USD 45 million reduction in EBITDA."}],
            }]}, "draft")]
        result = self.run_model(FakeModel(*calls))
        self.assertEqual(result.termination_reason, "draft_finding")
        self.assertEqual(result.outcome.rule.severity, "critical")
        self.assertEqual(result.outcome.rule.prior_leverage, "3.75")
        self.assertTrue(result.outcome.requires_human_review)
        self.assertEqual(result.tool_calls, 7)

    def test_irrelevant_retrieval_can_end_in_explicit_review(self):
        index_case_document(self.session, self.context, self.document_id, self.backend)
        model = FakeModel(turn("search_documents", {"query": "volcano astronomy"}),
            turn("request_human_review", {"reason": "No relevant evidence"}, "review"))
        result = self.run_model(model)
        self.assertEqual(result.termination_reason, "human_review")
        self.assertEqual(json.loads(model.requests[1][-1]["content"])["data"]["matches"], [])

    def test_unindexed_missing_evidence_can_end_in_review(self):
        result = self.run_model(FakeModel(turn("search_documents", {"query": "EBITDA"}),
            turn("request_human_review", {"reason": "No indexed supporting evidence"}, "review")))
        self.assertEqual(result.termination_reason, "human_review")
        self.assertEqual(result.steps[1].result["error"]["code"], "index_unavailable")
        self.assertEqual(self.backend.calls, [])

    def test_scope_override_arguments_are_rejected_and_not_executed(self):
        result = self.run_model(FakeModel(
            turn("get_current_fact", {"metric": "ebitda", "borrower_id": str(uuid.uuid4())}),
            turn("request_human_review", {"reason": "Invalid request"}, "review")))
        self.assertEqual(result.steps[1].result["error"]["code"], "invalid_arguments")
        self.assertEqual(result.termination_reason, "human_review")

    def test_wrong_case_is_rejected_before_any_model_or_embedding_call(self):
        wrong_context = CaseContext(**(self.context.model_dump() | {"borrower_id": uuid.uuid4()}))
        model = FakeModel()
        result = run_investigation(self.session, wrong_context, model, embeddings=self.backend)
        self.assertEqual(result.termination_reason, "scope_unavailable")
        self.assertEqual(model.requests, [])
        self.assertEqual(self.backend.calls, [])

    def test_fabricated_quote_is_rejected_then_model_can_escalate(self):
        result = self.run_model(FakeModel(
            turn("get_document_page", self.page_args),
            turn("create_finding", {"claims": [{"text": "Unsupported claim", "citations": [
                self.page_args | {"quote": "Management says aliens caused the decline."},
            ]}]}, "draft"),
            turn("request_human_review", {"reason": "Unable to support the claim"}, "review")))
        self.assertEqual(result.termination_reason, "human_review")
        self.assertEqual(result.steps[3].result["error"]["code"], "invalid_citation")


if __name__ == "__main__":
    unittest.main()
