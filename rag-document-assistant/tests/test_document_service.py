import hashlib
import unittest
import uuid

from app.chunking import TextChunk
from app.document_service import (
    _validate_document_content,
    build_chunk_id,
    build_document_id,
    build_page_id,
)
from app.ingestion import PageText


class DeterministicDocumentIdentityTests(unittest.TestCase):
    def test_ids_are_stable_and_hierarchical(self):
        tenant_id = uuid.uuid4()
        content_hash = hashlib.sha256(b"document").hexdigest()

        document_id = build_document_id(tenant_id, content_hash)

        self.assertEqual(document_id, build_document_id(tenant_id, content_hash))
        self.assertEqual(build_page_id(document_id, 1), build_page_id(document_id, 1))
        self.assertEqual(build_chunk_id(document_id, 0), build_chunk_id(document_id, 0))
        self.assertNotEqual(build_page_id(document_id, 1), build_page_id(document_id, 2))
        self.assertNotEqual(build_chunk_id(document_id, 0), build_chunk_id(document_id, 1))


class DocumentContentValidationTests(unittest.TestCase):
    def setUp(self):
        self.pages = [PageText(page_number=1, text="First page")]
        self.chunks = [
            TextChunk(
                text="First page",
                start_page=1,
                end_page=1,
                page_span=[1],
                chunk_index=0,
            )
        ]

    def test_requires_one_embedding_per_chunk(self):
        with self.assertRaisesRegex(ValueError, "exactly one embedding"):
            _validate_document_content(
                pages=self.pages,
                chunks=self.chunks,
                embeddings=[],
            )

    def test_chunk_pages_must_exist(self):
        invalid_chunk = TextChunk(
            text="Missing page",
            start_page=2,
            end_page=2,
            page_span=[2],
            chunk_index=0,
        )

        with self.assertRaisesRegex(ValueError, "persisted pages"):
            _validate_document_content(
                pages=self.pages,
                chunks=[invalid_chunk],
                embeddings=[[1.0] * 768],
            )


if __name__ == "__main__":
    unittest.main()
