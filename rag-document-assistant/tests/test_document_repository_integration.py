import hashlib
import os
import unittest
import uuid
from datetime import date

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.chunking import TextChunk
from app.document_repository import get_document_page, search_similar_chunks
from app.document_service import build_document_id, persist_document
from app.ingestion import PageText
from app.models import (
    Borrower,
    Document,
    DocumentChunk,
    DocumentPage,
    ReportingPeriod,
    Tenant,
)


@unittest.skipUnless(
    os.getenv("RUN_DATABASE_TESTS") == "1",
    "Set RUN_DATABASE_TESTS=1 to run PostgreSQL integration tests",
)
class PostgreSQLDocumentStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.database import SessionLocal

        cls.SessionLocal = SessionLocal
        cls.tenant_id = uuid.uuid4()
        cls.borrower_id = uuid.uuid4()
        cls.reporting_period_id = uuid.uuid4()

        with cls.SessionLocal.begin() as session:
            session.add(
                Tenant(
                    id=cls.tenant_id,
                    name="PostgreSQL repository test tenant",
                )
            )
            session.flush()
            session.add(
                Borrower(
                    id=cls.borrower_id,
                    tenant_id=cls.tenant_id,
                    legal_name="Repository Test Borrower",
                    external_id=f"repo-test-{cls.borrower_id}",
                )
            )
            session.flush()
            session.add(
                ReportingPeriod(
                    id=cls.reporting_period_id,
                    tenant_id=cls.tenant_id,
                    borrower_id=cls.borrower_id,
                    period_type="quarter",
                    period_start=date(2026, 4, 1),
                    period_end=date(2026, 6, 30),
                    fiscal_year=2026,
                    fiscal_quarter=2,
                    label="Q2 2026 repository test",
                )
            )

    @classmethod
    def tearDownClass(cls):
        with cls.SessionLocal.begin() as session:
            document_ids = select(Document.id).where(
                Document.tenant_id == cls.tenant_id
            )
            session.execute(
                delete(DocumentChunk).where(
                    DocumentChunk.document_id.in_(document_ids)
                )
            )
            session.execute(
                delete(DocumentPage).where(
                    DocumentPage.document_id.in_(document_ids)
                )
            )
            session.execute(delete(Document).where(Document.tenant_id == cls.tenant_id))
            session.execute(
                delete(ReportingPeriod).where(
                    ReportingPeriod.id == cls.reporting_period_id
                )
            )
            session.execute(delete(Borrower).where(Borrower.id == cls.borrower_id))
            session.execute(delete(Tenant).where(Tenant.id == cls.tenant_id))

    def test_persists_pages_and_chunks_and_searches_with_tenant_scope(self):
        content_hash = hashlib.sha256(b"search document").hexdigest()
        pages = [
            PageText(page_number=1, text="Debt increased during the quarter."),
            PageText(page_number=2, text="EBITDA declined during the quarter."),
        ]
        chunks = [
            TextChunk("Debt increased.", 1, 1, [1], 0),
            TextChunk("EBITDA declined.", 2, 2, [2], 1),
        ]
        embeddings = [
            [1.0] + [0.0] * 767,
            [0.0, 1.0] + [0.0] * 766,
        ]

        with self.SessionLocal() as session:
            result = persist_document(
                session,
                tenant_id=self.tenant_id,
                borrower_id=self.borrower_id,
                reporting_period_id=self.reporting_period_id,
                original_filename="search.pdf",
                content_hash=content_hash,
                byte_size=100,
                pages=pages,
                chunks=chunks,
                embeddings=embeddings,
                embedding_model="test-embedding-model",
            )

        self.assertEqual(result.outcome, "created")
        self.assertEqual(
            result.document_id,
            build_document_id(self.tenant_id, content_hash),
        )
        self.assertEqual(result.page_count, 2)
        self.assertEqual(result.chunk_count, 2)

        with self.SessionLocal() as session:
            page = get_document_page(
                session,
                tenant_id=self.tenant_id,
                document_id=result.document_id,
                page_number=2,
            )
            matches = search_similar_chunks(
                session,
                tenant_id=self.tenant_id,
                document_id=result.document_id,
                query_embedding=embeddings[0],
                top_k=2,
                max_distance=None,
            )
            cross_tenant_matches = search_similar_chunks(
                session,
                tenant_id=uuid.uuid4(),
                query_embedding=embeddings[0],
                top_k=2,
                max_distance=None,
            )

        self.assertIsNotNone(page)
        self.assertEqual(page.text_content, pages[1].text)
        self.assertEqual([match.chunk_index for match in matches], [0, 1])
        self.assertAlmostEqual(matches[0].distance, 0.0)
        self.assertEqual(cross_tenant_matches, [])

    def test_skips_duplicates_and_replaces_content_when_forced(self):
        content_hash = hashlib.sha256(b"reprocessing document").hexdigest()
        original_pages = [PageText(page_number=1, text="Original page")]
        replacement_pages = [PageText(page_number=1, text="Replacement page")]
        original_chunks = [TextChunk("Original", 1, 1, [1], 0)]
        replacement_chunks = [TextChunk("Replacement", 1, 1, [1], 0)]
        embedding = [[1.0] + [0.0] * 767]

        with self.SessionLocal() as session:
            created = persist_document(
                session,
                tenant_id=self.tenant_id,
                borrower_id=self.borrower_id,
                reporting_period_id=self.reporting_period_id,
                original_filename="reprocess.pdf",
                content_hash=content_hash,
                byte_size=100,
                pages=original_pages,
                chunks=original_chunks,
                embeddings=embedding,
                embedding_model="test-embedding-model",
            )
        with self.SessionLocal() as session:
            skipped = persist_document(
                session,
                tenant_id=self.tenant_id,
                borrower_id=self.borrower_id,
                reporting_period_id=self.reporting_period_id,
                original_filename="reprocess.pdf",
                content_hash=content_hash,
                byte_size=100,
                pages=replacement_pages,
                chunks=replacement_chunks,
                embeddings=embedding,
                embedding_model="test-embedding-model",
            )
        with self.SessionLocal() as session:
            reprocessed = persist_document(
                session,
                tenant_id=self.tenant_id,
                borrower_id=self.borrower_id,
                reporting_period_id=self.reporting_period_id,
                original_filename="reprocess.pdf",
                content_hash=content_hash,
                byte_size=100,
                pages=replacement_pages,
                chunks=replacement_chunks,
                embeddings=embedding,
                embedding_model="test-embedding-model",
                force_reprocess=True,
            )

        self.assertEqual(created.outcome, "created")
        self.assertEqual(skipped.outcome, "skipped")
        self.assertEqual(reprocessed.outcome, "reprocessed")
        self.assertEqual(created.document_id, reprocessed.document_id)

        with self.SessionLocal() as session:
            page = get_document_page(
                session,
                tenant_id=self.tenant_id,
                document_id=created.document_id,
                page_number=1,
            )
            page_count = session.scalar(
                select(func.count())
                .select_from(DocumentPage)
                .where(DocumentPage.document_id == created.document_id)
            )
            chunk_count = session.scalar(
                select(func.count())
                .select_from(DocumentChunk)
                .where(DocumentChunk.document_id == created.document_id)
            )

        self.assertEqual(page.text_content, "Replacement page")
        self.assertEqual(page_count, 1)
        self.assertEqual(chunk_count, 1)

    def test_failed_reprocessing_rolls_back_to_previous_content(self):
        content_hash = hashlib.sha256(b"rollback document").hexdigest()
        pages = [PageText(page_number=1, text="Content that must survive")]
        chunks = [TextChunk("Surviving content", 1, 1, [1], 0)]
        embeddings = [[1.0] + [0.0] * 767]

        with self.SessionLocal() as session:
            created = persist_document(
                session,
                tenant_id=self.tenant_id,
                borrower_id=self.borrower_id,
                reporting_period_id=self.reporting_period_id,
                original_filename="rollback.pdf",
                content_hash=content_hash,
                byte_size=100,
                pages=pages,
                chunks=chunks,
                embeddings=embeddings,
                embedding_model="test-embedding-model",
            )

        with self.SessionLocal() as session:
            with self.assertRaises(IntegrityError):
                persist_document(
                    session,
                    tenant_id=self.tenant_id,
                    borrower_id=self.borrower_id,
                    reporting_period_id=self.reporting_period_id,
                    original_filename="rollback.pdf",
                    content_hash=content_hash,
                    byte_size=100,
                    pages=[PageText(page_number=1, text="Replacement")],
                    chunks=[TextChunk("Replacement", 1, 1, [1], 0)],
                    embeddings=embeddings,
                    embedding_model="test-embedding-model",
                    document_type="invalid",
                    force_reprocess=True,
                )

        with self.SessionLocal() as session:
            page = get_document_page(
                session,
                tenant_id=self.tenant_id,
                document_id=created.document_id,
                page_number=1,
            )

        self.assertEqual(page.text_content, "Content that must survive")


if __name__ == "__main__":
    unittest.main()
