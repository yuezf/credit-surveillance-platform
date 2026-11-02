import unittest
import uuid
from unittest.mock import MagicMock

from pydantic import ValidationError

from app.case_tools import CaseContext
from app.credit_indexing import EmbeddingError, embed_validated
from app.investigation_tools import FindingRequest, HumanReviewRequest, InvestigationTools, SearchRequest


class InvestigationContractTests(unittest.TestCase):
    def setUp(self):
        self.session = MagicMock()
        self.context = CaseContext(**{name: uuid.uuid4() for name in (
            "tenant_id", "borrower_id", "prior_period_id", "current_period_id"
        )})
        self.tools = InvestigationTools(self.session, self.context)

    def test_search_rejects_blank_queries_limits_and_scope_overrides_without_queries(self):
        for arguments in (
            {"query": " "}, {"query": "x" * 501}, {"query": "EBITDA", "top_k": 6},
            {"query": "EBITDA", "top_k": True}, {"query": "EBITDA", "top_k": "2"},
            {"query": "EBITDA", "tenant_id": str(uuid.uuid4())},
            {"query": "EBITDA", "reporting_period_id": str(uuid.uuid4())},
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.tools.search_documents(arguments).error.code, "invalid_arguments")
        self.assertEqual(self.session.mock_calls, [])

    def test_finding_requires_claims_and_citations_and_forbids_rule_overrides(self):
        citation = {"document_id": str(uuid.uuid4()), "page_number": 2, "quote": "Evidence"}
        valid = {"claims": [{"text": "Explanation", "citations": [citation]}]}
        for arguments in (
            {"claims": []}, {"claims": [{"text": "x", "citations": []}]},
            valid | {"severity": "critical"}, valid | {"current_leverage": "999"},
            {"claims": [{"text": " ", "citations": [citation]}]},
            {"claims": [{"text": "x", "citations": [citation | {"page_number": True}]}]},
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.tools.create_finding(arguments).error.code, "invalid_arguments")
        self.assertEqual(self.session.mock_calls, [])

    def test_review_requires_a_reason_and_rejects_case_overrides(self):
        for arguments in ({"reason": " "}, {"reason": "Missing evidence", "borrower_id": str(uuid.uuid4())}):
            self.assertEqual(self.tools.request_human_review(arguments).error.code, "invalid_arguments")
        self.assertEqual(self.session.mock_calls, [])

    def test_model_facing_schemas_exclude_context_and_calculation_controls(self):
        self.assertEqual(set(SearchRequest.model_json_schema()["properties"]), {"query", "top_k"})
        self.assertEqual(set(FindingRequest.model_json_schema()["properties"]), {"claims"})
        self.assertEqual(set(HumanReviewRequest.model_json_schema()["properties"]), {"reason", "unresolved_questions"})

    def test_invalid_embedding_count_dimension_and_values_are_rejected(self):
        backend = MagicMock(model_name="test-model")
        for result in ([], [[1.0]], [[0.0] * 768], [[float("nan")] * 768], [[1e308] * 768]):
            backend.embed.return_value = result
            with self.subTest(result_length=len(result)), self.assertRaises(EmbeddingError):
                embed_validated(backend, ["query"])

    def test_embedding_errors_do_not_expose_provider_details(self):
        backend = MagicMock(model_name="test-model")
        backend.embed.side_effect = RuntimeError("private credential or endpoint")
        with self.assertRaises(EmbeddingError) as caught:
            embed_validated(backend, ["query"])
        self.assertNotIn("private", str(caught.exception))

    def test_distance_cutoff_is_server_validated(self):
        for distance in (-1, 3, float("nan"), float("inf")):
            with self.subTest(distance=distance), self.assertRaises(ValueError):
                InvestigationTools(self.session, self.context, max_distance=distance)


if __name__ == "__main__":
    unittest.main()
