"""Deterministic fake vectors for tests only, not semantic-quality evaluation."""


class FixtureEmbeddings:
    model_name = "test-fixture-vectors-v1"

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        result = []
        for text in texts:
            lower = text.lower()
            index = 0 if any(word in lower for word in ("decline", "volumes", "input costs")) else 1
            if any(word in lower for word in ("unrelated", "volcano", "astronomy")):
                index = 2
            vector = [0.0] * 768
            vector[index] = 1.0
            result.append(vector)
        return result
