"""Explicit adapter from the configured embedding service to case tools.

Importing this module needs no credentials. Constructing the adapter loads the
existing configuration/client, but only embed() can make an API request.
"""

from importlib import import_module

from app.schema_constants import DEFAULT_EMBEDDING_DIMENSION


class ConfiguredEmbeddingBackend:
    """Use one instance for both index_case_document and InvestigationTools.

    Configuration is loaded once at construction. To change the encoder, restart
    the process with new configuration and use a separate/rebuilt index.
    """

    def __init__(self):
        configuration = import_module("app.configure")
        if configuration.EMBEDDING_DIMENSION != DEFAULT_EMBEDDING_DIMENSION:
            raise ValueError(
                f"Case embeddings require {DEFAULT_EMBEDDING_DIMENSION} dimensions"
            )
        model_name = configuration.EMBEDDING_MODEL
        if not isinstance(model_name, str) or not model_name.strip() or len(model_name) > 200:
            raise ValueError("A nonempty embedding model identifier of at most 200 characters is required")
        service = import_module("app.embedding_service")
        self._model_name = model_name
        self._embed_in_batches = service.get_embeddings_in_batches

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Delegate batching/normalization; case callers validate and redact errors."""
        if not texts:
            return []
        return self._embed_in_batches(texts)
