import math
import unittest

from app.embedding_utils import normalize_embedding


class NormalizeEmbeddingTests(unittest.TestCase):
    def test_returns_l2_normalized_values(self):
        result = normalize_embedding([3.0, 4.0], dimension=2)

        self.assertEqual(result, [0.6, 0.8])
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in result)), 1.0)

    def test_rejects_wrong_dimension(self):
        with self.assertRaisesRegex(ValueError, "2 dimensions"):
            normalize_embedding([1.0], dimension=2)

    def test_rejects_zero_vector(self):
        with self.assertRaisesRegex(ValueError, "zero vector"):
            normalize_embedding([0.0, 0.0], dimension=2)

    def test_rejects_nonfinite_values(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            normalize_embedding([1.0, math.nan], dimension=2)


if __name__ == "__main__":
    unittest.main()
