import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.configure import EMBEDDING_DIMENSION, EMBEDDING_MODEL
from app.embedding_service import get_embeddings, get_embeddings_in_batches


class EmbeddingServiceTests(unittest.TestCase):
    @patch("app.embedding_service.client")
    def test_get_embeddings_requests_configured_dimension(self, mock_client):
        first = [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)
        second = [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSION - 2)
        mock_client.embeddings.create.return_value = SimpleNamespace(
            data=[
                SimpleNamespace(embedding=first),
                SimpleNamespace(embedding=second),
            ]
        )

        result = get_embeddings(["first chunk", "second chunk"])

        self.assertEqual(result, [first, second])
        mock_client.embeddings.create.assert_called_once_with(
            model=EMBEDDING_MODEL,
            input=["first chunk", "second chunk"],
            dimensions=EMBEDDING_DIMENSION,
        )

    @patch("app.embedding_service.get_embeddings")
    def test_get_embeddings_in_batches_preserves_order(self, mock_get_embeddings):
        mock_get_embeddings.side_effect = lambda texts: [
            [float(text)] for text in texts
        ]

        result = get_embeddings_in_batches(["1", "2", "3", "4", "5"], batch_size=2)

        self.assertEqual(result, [[1.0], [2.0], [3.0], [4.0], [5.0]])
        self.assertEqual(
            [call.args[0] for call in mock_get_embeddings.call_args_list],
            [["1", "2"], ["3", "4"], ["5"]],
        )

    @patch("app.embedding_service.client")
    def test_get_embeddings_rejects_wrong_dimension(self, mock_client):
        mock_client.embeddings.create.return_value = SimpleNamespace(
            data=[SimpleNamespace(embedding=[1.0, 2.0])]
        )

        with self.assertRaisesRegex(ValueError, "768 dimensions"):
            get_embeddings(["text"])


if __name__ == "__main__":
    unittest.main()
