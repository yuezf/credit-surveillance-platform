import unittest
import uuid
from unittest.mock import ANY, Mock, patch

from app.document_repository import ChunkSearchResult
from app.retrieval_service import search_similar_chunks


class RetrievalServiceTests(unittest.TestCase):
    @patch("app.retrieval_service.search_postgres_chunks")
    @patch("app.retrieval_service.get_embeddings")
    def test_maps_postgres_match_to_existing_rag_shape(
        self,
        mock_get_embeddings,
        mock_search_postgres_chunks,
    ):
        tenant_id = uuid.uuid4()
        document_id = uuid.uuid4()
        chunk_id = uuid.uuid4()
        query_embedding = [1.0] + [0.0] * 767
        mock_get_embeddings.return_value = [query_embedding]
        mock_search_postgres_chunks.return_value = [
            ChunkSearchResult(
                id=chunk_id,
                document_id=document_id,
                document_name="example.pdf",
                document_hash="a" * 64,
                chunk_index=2,
                text="Evidence text",
                start_page=3,
                end_page=4,
                distance=0.12,
            )
        ]

        result = search_similar_chunks(
            Mock(),
            query="Why did EBITDA decline?",
            tenant_id=tenant_id,
            document_id=document_id,
            top_k=5,
        )

        self.assertEqual(result[0]["id"], str(chunk_id))
        self.assertEqual(result[0]["text"], "Evidence text")
        self.assertEqual(result[0]["metadata"]["page_span"], [3, 4])
        self.assertEqual(result[0]["distance"], 0.12)
        mock_search_postgres_chunks.assert_called_once_with(
            ANY,
            tenant_id=tenant_id,
            query_embedding=query_embedding,
            top_k=5,
            document_id=document_id,
            borrower_id=None,
            reporting_period_id=None,
            max_distance=0.6,
        )


if __name__ == "__main__":
    unittest.main()
