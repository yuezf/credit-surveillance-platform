import json
import unittest
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from app.case_tools import (
    CaseContext, CaseReadTools, FactRequest, PageRequest, ToolError, ToolResult,
    serialize_fact,
)
from app.models import Document, DocumentPage, FinancialFact


class CaseToolContractTests(unittest.TestCase):
    def setUp(self):
        self.context = CaseContext(
            tenant_id=uuid.uuid4(), borrower_id=uuid.uuid4(),
            prior_period_id=uuid.uuid4(), current_period_id=uuid.uuid4(),
        )
        self.session = MagicMock()
        self.tools = CaseReadTools(self.session, self.context)

    def test_case_context_is_frozen_and_requires_server_uuid_objects(self):
        with self.assertRaises(ValidationError):
            self.context.borrower_id = uuid.uuid4()
        with self.assertRaises(ValidationError):
            CaseContext(**(self.context.model_dump() | {"tenant_id": str(self.context.tenant_id)}))

    def test_request_schemas_do_not_expose_case_identity(self):
        self.assertEqual(set(FactRequest.model_json_schema()["properties"]), {"metric"})
        self.assertEqual(set(PageRequest.model_json_schema()["properties"]), {"document_id", "page_number"})
        self.assertFalse(FactRequest.model_json_schema()["additionalProperties"])
        self.assertFalse(PageRequest.model_json_schema()["additionalProperties"])

    def test_malformed_fact_arguments_fail_before_any_database_access(self):
        for arguments in (
            {}, {"metric": "net_income"}, {"metric": 123}, {"metric": None},
            {"metric": "ebitda", "tenant_id": str(self.context.tenant_id)},
            {"metric": "ebitda", "borrower_id": str(uuid.uuid4())},
            {"metric": "ebitda", "reporting_period_id": str(uuid.uuid4())},
            None, [], '{"metric":"ebitda"}',
        ):
            for method in (self.tools.get_current_fact, self.tools.get_prior_fact):
                with self.subTest(arguments=arguments, method=method.__name__):
                    result = method(arguments)
                    self.assertFalse(result.ok)
                    self.assertEqual(result.error.code, "invalid_arguments")
        self.assertEqual(self.session.mock_calls, [])

    def test_malformed_page_arguments_fail_before_any_database_access(self):
        valid = {"document_id": str(uuid.uuid4()), "page_number": 1}
        for arguments in (
            {}, valid | {"document_id": "not-a-uuid"},
            valid | {"page_number": 0}, valid | {"page_number": -1},
            valid | {"page_number": True}, valid | {"page_number": "1"},
            valid | {"page_number": 1.0}, valid | {"tenant_id": str(uuid.uuid4())},
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.tools.get_document_page(arguments).error.code, "invalid_arguments")
        self.assertEqual(self.session.mock_calls, [])

    def test_scope_denial_is_structured_and_does_not_leak_internal_errors(self):
        with patch("app.case_tools.authorize_credit_case", side_effect=PermissionError("private detail")):
            result = self.tools.get_current_fact({"metric": "ebitda"})
        self.assertEqual(result.error.code, "scope_unavailable")
        self.assertNotIn("private detail", result.model_dump_json())
        self.session.scalar.assert_not_called()

    def test_database_errors_are_redacted_and_leave_session_management_to_caller(self):
        error = OperationalError("private SQL", {"secret": "private"}, Exception("private driver error"))
        with patch("app.case_tools.authorize_credit_case", side_effect=error):
            for result in (
                self.tools.get_current_fact({"metric": "ebitda"}),
                self.tools.get_document_page({"document_id": str(uuid.uuid4()), "page_number": 1}),
            ):
                self.assertEqual(result.error.code, "data_unavailable")
                self.assertNotIn("private", result.model_dump_json())
        self.session.commit.assert_not_called()
        self.session.rollback.assert_not_called()

    def test_results_require_exactly_one_success_payload_or_error(self):
        for fields in (
            {"ok": True}, {"ok": False}, {"ok": False, "data": {"value": "1"}},
            {"ok": True, "data": {}, "error": ToolError(code="invalid_fact", message="invalid")},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                ToolResult(**fields)

    def test_serializer_preserves_decimal_precision_identity_dates_and_provenance(self):
        document = Document(
            id=uuid.uuid4(), borrower_id=self.context.borrower_id,
            reporting_period_id=self.context.current_period_id,
            original_filename="synthetic.pdf", content_hash="a" * 64,
        )
        page = DocumentPage(id=uuid.uuid4(), page_number=2, text_hash="b" * 64)
        fact = FinancialFact(
            id=uuid.uuid4(), borrower_id=self.context.borrower_id,
            reporting_period_id=self.context.current_period_id,
            metric_name="ebitda", value=Decimal("330000000.123456"),
            currency="USD", unit="units", measurement_basis="ttm",
            measurement_start=date(2024, 7, 1), measurement_end=date(2025, 6, 30),
            source_text="Synthetic amount", is_canonical=True,
        )
        output = serialize_fact(fact, page, document)
        payload = output.model_dump(mode="json")
        self.assertEqual(payload["value"], "330000000.123456")
        self.assertEqual(payload["fact_id"], str(fact.id))
        self.assertEqual(payload["measurement_start"], "2024-07-01")
        self.assertEqual(payload["source"]["document_id"], str(document.id))
        self.assertEqual(payload["source"]["page_id"], str(page.id))
        self.assertEqual(payload["source_text"], fact.source_text)
        self.assertEqual(json.loads(json.dumps(payload)), payload)


if __name__ == "__main__":
    unittest.main()
