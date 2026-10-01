import hashlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.api import app
from app.chunking import TextChunk
from app.demo_seed import DEMO_TENANT_ID, seed_demo_scope
from app.ingestion import PageText
from app.models import Document


@unittest.skipUnless(
    os.getenv("RUN_DATABASE_TESTS") == "1",
    "Set RUN_DATABASE_TESTS=1 to run PostgreSQL API integration tests",
)
class PostgreSQLAPIIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.database import SessionLocal

        cls.SessionLocal = SessionLocal
        with cls.SessionLocal() as session:
            seed_demo_scope(session)
        cls.client = TestClient(app)
        cls.pdf_bytes = b"%PDF-postgres-api-integration"
        cls.content_hash = hashlib.sha256(cls.pdf_bytes).hexdigest()
        cls.embedding = [1.0] + [0.0] * 767

    @classmethod
    def tearDownClass(cls):
        with cls.SessionLocal.begin() as session:
            session.execute(
                delete(Document).where(
                    Document.tenant_id == DEMO_TENANT_ID,
                    Document.content_hash == cls.content_hash,
                )
            )

    @patch("app.api.get_embeddings_in_batches")
    @patch("app.api.chunking_across_pages")
    @patch("app.api.extract_pages_from_pdf")
    def test_ingest_search_and_answer_use_postgres(
        self,
        mock_extract_pages,
        mock_chunk_pages,
        mock_get_embeddings,
    ):
        mock_extract_pages.return_value = [
            PageText(page_number=1, text="EBITDA declined due to lower volume.")
        ]
        mock_chunk_pages.return_value = [
            TextChunk(
                "EBITDA declined due to lower volume.",
                1,
                1,
                [1],
                0,
            )
        ]
        mock_get_embeddings.return_value = [self.embedding]

        ingest_response = self.client.post(
            "/ingest",
            files={
                "files": (
                    "integration.pdf",
                    self.pdf_bytes,
                    "application/pdf",
                )
            },
        )
        self.assertEqual(ingest_response.status_code, 200)
        document_id = ingest_response.json()["documents"][0]["document_id"]

        with patch(
            "app.retrieval_service.get_embeddings",
            return_value=[self.embedding],
        ):
            search_response = self.client.post(
                "/search",
                json={
                    "queries": ["Why did EBITDA decline?"],
                    "top_k": 5,
                    "document_id": document_id,
                    "max_distance": 0.6,
                },
            )

        self.assertEqual(search_response.status_code, 200)
        matches = search_response.json()["query-0"]["result"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["metadata"]["document_id"], document_id)
        self.assertEqual(matches[0]["metadata"]["start_page"], 1)

        chat_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="EBITDA declined because of lower volume."
                    )
                )
            ]
        )
        with (
            patch(
                "app.retrieval_service.get_embeddings",
                return_value=[self.embedding],
            ),
            patch(
                "app.rag.client.chat.completions.create",
                return_value=chat_response,
            ),
        ):
            answer_response = self.client.post(
                "/answer",
                json={
                    "query": "Why did EBITDA decline?",
                    "top_k": 5,
                    "document_id": document_id,
                },
            )

        self.assertEqual(answer_response.status_code, 200)
        self.assertEqual(
            answer_response.json()["answer"],
            "EBITDA declined because of lower volume.",
        )
        self.assertEqual(
            answer_response.json()["sources"][0]["document_id"],
            document_id,
        )


if __name__ == "__main__":
    unittest.main()
