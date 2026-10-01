import math
from collections.abc import Sequence

from app.schema_constants import DEFAULT_EMBEDDING_DIMENSION


def normalize_embedding(
    embedding: Sequence[float],
    dimension: int = DEFAULT_EMBEDDING_DIMENSION,
) -> list[float]:
    """Validate an embedding and return its L2-normalized values."""
    if len(embedding) != dimension:
        raise ValueError(
            f"Expected an embedding with {dimension} dimensions, got {len(embedding)}"
        )

    values = [float(value) for value in embedding]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Embedding values must be finite numbers")

    magnitude = math.sqrt(sum(value * value for value in values))
    if magnitude == 0:
        raise ValueError("Embedding cannot be the zero vector")

    return [value / magnitude for value in values]
