import unittest
import uuid
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.api import app
from app.auth import get_current_tenant_id
from app.chunking import TextChunk
from app.database import get_db_session
from app.document_service import DocumentPersistenceResult
from app.ingestion import PageText


class PostgreSQLAPIWiringTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.tenant_id = uuid.uuid4()

        def override_database_session():
            yield self.session

        app.dependency_overrides[get_db_session] = override_database_session
        app.dependency_overrides[get_current_tenant_id] = lambda: self.tenant_id
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    @patch("app.api.persist_document")
    @patch("app.api.get_embeddings_in_batches")
    @patch("app.api.chunking_across_pages")
    @patch("app.api.extract_pages_from_pdf")
    @patch("app.api.get_document_by_hash", return_value=None)
    @patch("app.api.credit_scope_exists", return_value=True)
    def test_ingest_routes_prepared_content_to_postgres_service(
        self,
        _mock_scope,
        _mock_existing,
        mock_extract_pages,
        mock_chunk_pages,
        mock_get_embeddings,
        mock_persist_document,
    ):
        document_id = uuid.uuid4()
        pages = [PageText(page_number=1, text="Page text")]
        chunks = [TextChunk("Page text", 1, 1, [1], 0)]
        embedding = [1.0] + [0.0] * 767
        mock_extract_pages.return_value = pages
        mock_chunk_pages.return_value = chunks
        mock_get_embeddings.return_value = [embedding]
        mock_persist_document.return_value = DocumentPersistenceResult(
            document_id=document_id,
            outcome="created",
            page_count=1,
            chunk_count=1,
        )

        response = self.client.post(
            "/ingest",
            data={
                "borrower_id": str(uuid.uuid4()),
                "reporting_period_id": str(uuid.uuid4()),
            },
            files={"files": ("example.pdf", b"%PDF-test", "application/pdf")},
        )

        self.assertEqual(response.status_code, 200)
        document = response.json()["documents"][0]
        self.assertEqual(document["document_id"], str(document_id))
        self.assertEqual(document["status"], "newly_ingested")
        mock_get_embeddings.assert_called_once_with(["Page text"])
        mock_persist_document.assert_called_once()
        self.assertEqual(mock_persist_document.call_args.kwargs["tenant_id"], self.tenant_id)

    @patch("app.api.search_similar_chunks", return_value=[])
    def test_search_uses_postgres_retrieval_service(self, mock_search):
        response = self.client.post(
            "/search",
            json={"queries": ["What changed?"], "top_k": 5},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["query-0"]["result"], [])
        mock_search.assert_called_once()
        self.assertIs(mock_search.call_args.args[0], self.session)
        self.assertEqual(mock_search.call_args.kwargs["tenant_id"], self.tenant_id)

    @patch("app.api.generate_answer")
    def test_answer_uses_postgres_backed_rag_service(self, mock_generate_answer):
        mock_generate_answer.return_value = {
            "query": "Why?",
            "answer": "Because EBITDA declined.",
            "sources": [],
        }

        response = self.client.post(
            "/answer",
            json={"query": "Why?", "top_k": 5},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], "Because EBITDA declined.")
        mock_generate_answer.assert_called_once()
        self.assertIs(mock_generate_answer.call_args.args[0], self.session)
        self.assertEqual(mock_generate_answer.call_args.kwargs["tenant_id"], self.tenant_id)

    def test_ingest_rejects_nonprogressing_chunk_configuration(self):
        response = self.client.post(
            "/ingest",
            data={
                "borrower_id": str(uuid.uuid4()),
                "reporting_period_id": str(uuid.uuid4()),
                "chunk_size": 100,
                "overlap": 100,
            },
            files={"files": ("example.pdf", b"%PDF-test", "application/pdf")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "overlap must be smaller than chunk_size",
        )


if __name__ == "__main__":
    unittest.main()
