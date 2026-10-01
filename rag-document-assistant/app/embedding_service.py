from app.configure import EMBEDDING_DIMENSION, EMBEDDING_MODEL
from app.embedding_utils import normalize_embedding
from app.model_client import client


EMBEDDING_BATCH_SIZE = 100


def get_embeddings(texts: list[str]) -> list[list[float]]:
    """Generate validated, normalized embeddings in input order."""
    if not texts:
        return []

    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=texts,
        dimensions=EMBEDDING_DIMENSION,
    )
    embeddings = [item.embedding for item in response.data]

    if len(embeddings) != len(texts):
        raise ValueError(
            "Embedding API returned a different number of vectors than input texts"
        )

    return [normalize_embedding(embedding) for embedding in embeddings]


def get_embeddings_in_batches(
    texts: list[str],
    batch_size: int = EMBEDDING_BATCH_SIZE,
) -> list[list[float]]:
    """Embed texts in bounded batches while preserving their input order."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")

    embeddings: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        embeddings.extend(get_embeddings(texts[start : start + batch_size]))

    return embeddings
