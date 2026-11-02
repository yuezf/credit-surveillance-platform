import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.credit_indexing import EmbeddingError, embed_validated
from app.embedding_backend import ConfiguredEmbeddingBackend


class ConfiguredEmbeddingBackendTests(unittest.TestCase):
    def setUp(self):
        self.configuration = SimpleNamespace(EMBEDDING_MODEL="configured-encoder", EMBEDDING_DIMENSION=768)
        self.batch_embed = Mock()
        self.service = SimpleNamespace(get_embeddings_in_batches=self.batch_embed)
        self.imports = patch("app.embedding_backend.import_module", side_effect={
            "app.configure": self.configuration,
            "app.embedding_service": self.service,
        }.__getitem__)
        self.import_module = self.imports.start()
        self.addCleanup(self.imports.stop)

    def test_import_does_not_load_configuration_or_client(self):
        subprocess.run([sys.executable, "-c", (
            "import sys; import app.embedding_backend; "
            "assert not {'app.configure', 'app.embedding_service', 'app.model_client'} & sys.modules.keys()"
        )], check=True, capture_output=True, text=True)

    def test_construction_loads_service_without_embedding(self):
        backend = ConfiguredEmbeddingBackend()
        self.assertEqual(backend.model_name, "configured-encoder")
        self.batch_embed.assert_not_called()

    def test_model_identifier_cannot_be_assigned(self):
        backend = ConfiguredEmbeddingBackend()
        with self.assertRaises(AttributeError):
            backend.model_name = "different-model"

    def test_embed_delegates_to_existing_batch_service_in_input_order(self):
        first = [1.0] + [0.0] * 767
        second = [0.0, 1.0] + [0.0] * 766
        self.batch_embed.return_value = [first, second]
        backend = ConfiguredEmbeddingBackend()
        self.assertEqual(embed_validated(backend, ["first", "second"]), [first, second])
        self.batch_embed.assert_called_once_with(["first", "second"])

    def test_empty_input_makes_no_embedding_call(self):
        self.assertEqual(ConfiguredEmbeddingBackend().embed([]), [])
        self.batch_embed.assert_not_called()

    def test_wrong_configured_dimension_is_rejected_before_loading_service(self):
        self.configuration.EMBEDDING_DIMENSION = 1536
        with self.assertRaisesRegex(ValueError, "768 dimensions"):
            ConfiguredEmbeddingBackend()
        self.assertEqual(self.import_module.call_count, 1)
        self.batch_embed.assert_not_called()

    def test_invalid_model_identifier_is_rejected_before_loading_service(self):
        for name in (None, "", " ", "x" * 201):
            with self.subTest(name=name):
                self.configuration.EMBEDDING_MODEL = name
                self.import_module.reset_mock()
                with self.assertRaises(ValueError):
                    ConfiguredEmbeddingBackend()
                self.assertEqual(self.import_module.call_count, 1)
        self.batch_embed.assert_not_called()

    def test_provider_error_is_redacted_by_case_boundary(self):
        self.batch_embed.side_effect = RuntimeError("private provider details")
        with self.assertRaises(EmbeddingError) as caught:
            embed_validated(ConfiguredEmbeddingBackend(), ["query"])
        self.assertNotIn("private", str(caught.exception))

    def test_invalid_provider_vectors_are_rejected_by_case_boundary(self):
        backend = ConfiguredEmbeddingBackend()
        for vectors in ([], [[1.0]], [[0.0] * 768], [[float("nan")] * 768]):
            with self.subTest(count=len(vectors)):
                self.batch_embed.return_value = vectors
                with self.assertRaises(EmbeddingError):
                    embed_validated(backend, ["query"])


if __name__ == "__main__":
    unittest.main()
