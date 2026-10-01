import hashlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app.api import app
from app.auth import issue_api_key
from app.chunking import TextChunk
from app.demo_seed import (
    DEMO_BORROWER_ID,
    DEMO_CURRENT_PERIOD_ID,
    DEMO_TENANT_ID,
    seed_demo_scope,
)
from app.ingestion import PageText
from app.models import Document, Tenant, TenantAPIKey
from app.original_storage import LocalOriginalPDFStore


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
            cls.demo_key = issue_api_key(session, DEMO_TENANT_ID, "API integration test")
            cls.other_tenant_id = uuid.uuid4()
            session.add(Tenant(id=cls.other_tenant_id, name="Other API test tenant"))
            session.commit()
            cls.other_key = issue_api_key(
                session, cls.other_tenant_id, "Other API integration test"
            )
        cls.client = TestClient(app)
        cls.storage_dir = tempfile.TemporaryDirectory()
        cls.storage_patcher = patch(
            "app.api.get_original_pdf_store",
            return_value=LocalOriginalPDFStore(Path(cls.storage_dir.name)),
        )
        cls.storage_patcher.start()
        cls.pdf_bytes = b"%PDF-postgres-api-integration"
        cls.content_hash = hashlib.sha256(cls.pdf_bytes).hexdigest()
        cls.embedding = [1.0] + [0.0] * 767

    @classmethod
    def tearDownClass(cls):
        cls.storage_patcher.stop()
        cls.storage_dir.cleanup()
        with cls.SessionLocal.begin() as session:
            session.execute(
                delete(Document).where(
                    Document.tenant_id == DEMO_TENANT_ID,
                    Document.content_hash == cls.content_hash,
                )
            )
            session.execute(
                delete(TenantAPIKey).where(
                    TenantAPIKey.key_hash.in_(
                        [
                            hashlib.sha256(cls.demo_key.encode()).hexdigest(),
                            hashlib.sha256(cls.other_key.encode()).hexdigest(),
                        ]
                    )
                )
            )
            session.execute(delete(Tenant).where(Tenant.id == cls.other_tenant_id))

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
            headers={"X-API-Key": self.demo_key},
            data={
                "borrower_id": str(DEMO_BORROWER_ID),
                "reporting_period_id": str(DEMO_CURRENT_PERIOD_ID),
            },
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

        with self.SessionLocal() as session:
            document = session.get(Document, uuid.UUID(document_id))
            self.assertTrue(document.storage_uri.startswith("file://"))
            self.assertEqual(
                Path(document.storage_uri.removeprefix("file://")).read_bytes(),
                self.pdf_bytes,
            )

        with patch(
            "app.retrieval_service.get_embeddings",
            return_value=[self.embedding],
        ):
            search_response = self.client.post(
                "/search",
                headers={"X-API-Key": self.demo_key},
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
                headers={"X-API-Key": self.demo_key},
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

        with patch("app.retrieval_service.get_embeddings", return_value=[self.embedding]):
            other_search = self.client.post(
                "/search",
                headers={"X-API-Key": self.other_key},
                json={
                    "queries": ["Why did EBITDA decline?"],
                    "top_k": 5,
                    "document_id": document_id,
                },
            )
        self.assertEqual(other_search.status_code, 200)
        self.assertEqual(other_search.json()["query-0"]["result"], [])
        self.assertEqual(
            other_search.json()["query-0"]["tenant_id"], str(self.other_tenant_id)
        )

        with (
            patch("app.retrieval_service.get_embeddings", return_value=[self.embedding]),
            patch("app.rag.client.chat.completions.create", return_value=chat_response),
        ):
            other_answer = self.client.post(
                "/answer",
                headers={"X-API-Key": self.other_key},
                json={
                    "query": "Why did EBITDA decline?",
                    "top_k": 5,
                    "document_id": document_id,
                },
            )
        self.assertEqual(other_answer.status_code, 200)
        self.assertEqual(other_answer.json()["sources"], [])

        other_ingest = self.client.post(
            "/ingest",
            headers={"X-API-Key": self.other_key},
            data={
                "borrower_id": str(DEMO_BORROWER_ID),
                "reporting_period_id": str(DEMO_CURRENT_PERIOD_ID),
            },
            files={"files": ("other.pdf", b"%PDF-other", "application/pdf")},
        )
        self.assertEqual(other_ingest.status_code, 400)

        other_counts = self.client.get(
            "/collection-info", headers={"X-API-Key": self.other_key}
        )
        self.assertEqual(other_counts.status_code, 200)
        self.assertEqual(other_counts.json()["document_count"], 0)

    def test_revoked_key_is_rejected(self):
        key_hash = hashlib.sha256(self.other_key.encode()).hexdigest()
        with self.SessionLocal.begin() as session:
            key_id = session.scalar(
                select(TenantAPIKey.id).where(TenantAPIKey.key_hash == key_hash)
            )
            session.execute(
                update(TenantAPIKey)
                .where(TenantAPIKey.id == key_id)
                .values(is_active=False)
            )
        try:
            response = self.client.get(
                "/collection-info", headers={"X-API-Key": self.other_key}
            )
            self.assertEqual(response.status_code, 401)
        finally:
            with self.SessionLocal.begin() as session:
                session.execute(
                    update(TenantAPIKey)
                    .where(TenantAPIKey.id == key_id)
                    .values(is_active=True)
                )


if __name__ == "__main__":
    unittest.main()
