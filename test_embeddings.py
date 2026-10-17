import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from hello_agents import (
    BaseEmbedder,
    EmbeddingConfig,
    EmbeddingError,
    HashingEmbedder,
    LocalSentenceTransformerEmbedder,
    cosine_similarity,
    create_embedder,
)


class FakeSentenceTransformer:
    def __init__(self):
        self.calls = []

    def get_sentence_embedding_dimension(self):
        return 3

    def encode(self, texts):
        self.calls.append(list(texts))
        return [
            [float(len(text)), float(text.count("a") + 1), 2.0]
            for text in texts
        ]


class FailingSentenceTransformer:
    def encode(self, texts):
        raise RuntimeError("secret model detail")


class BadSentenceTransformer:
    def __init__(self, output):
        self.output = output

    def encode(self, texts):
        return self.output


class TestEmbeddingConfig(unittest.TestCase):
    def test_safe_offline_defaults(self):
        config = EmbeddingConfig()

        self.assertEqual(config.provider, "hashing")
        self.assertEqual(config.dimension, 384)
        self.assertTrue(config.normalize)
        self.assertIsNone(config.model_path)

    def test_rejects_unknown_fields_invalid_dimensions_and_missing_path(self):
        with self.assertRaises(ValidationError):
            EmbeddingConfig(provider="unknown")
        with self.assertRaises(ValidationError):
            EmbeddingConfig(dimension=0)
        with self.assertRaises(ValidationError):
            EmbeddingConfig(unexpected=True)
        with self.assertRaises(ValidationError):
            EmbeddingConfig(provider="sentence_transformer")

    def test_normalizes_path_objects(self):
        config = EmbeddingConfig(
            provider="sentence_transformer",
            model_path=Path("models/local"),
        )

        self.assertEqual(config.model_path, str(Path("models/local")))


class TestBaseEmbeddingUtilities(unittest.TestCase):
    def test_base_embedder_is_abstract(self):
        with self.assertRaises(TypeError):
            BaseEmbedder()

    def test_cosine_similarity_handles_common_vectors(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)
        self.assertAlmostEqual(cosine_similarity([1, 0], [-1, 0]), -1.0)
        self.assertEqual(cosine_similarity([0, 0], [1, 0]), 0.0)

    def test_cosine_similarity_rejects_invalid_vectors(self):
        with self.assertRaises(EmbeddingError):
            cosine_similarity([], [])
        with self.assertRaises(EmbeddingError):
            cosine_similarity([1, 2], [1])
        with self.assertRaises(EmbeddingError):
            cosine_similarity([1, float("nan")], [1, 2])
        with self.assertRaises(EmbeddingError):
            cosine_similarity([True], [1])


class TestHashingEmbedder(unittest.TestCase):
    def test_is_deterministic_normalized_and_fixed_dimension(self):
        embedder = HashingEmbedder(128)

        first = embedder.embed("Python 智能体 memory")
        second = embedder.embed("Python 智能体 memory")

        self.assertEqual(first, second)
        self.assertEqual(len(first), 128)
        self.assertAlmostEqual(
            math.sqrt(sum(value * value for value in first)),
            1.0,
        )

    def test_shared_terms_score_above_unrelated_terms(self):
        embedder = HashingEmbedder(512)
        query = embedder.embed("python agent")
        related = embedder.embed("python memory")
        unrelated = embedder.embed("banana orange")

        self.assertGreater(
            cosine_similarity(query, related),
            cosine_similarity(query, unrelated),
        )

    def test_batch_preserves_order_and_supports_chinese(self):
        embedder = HashingEmbedder(64)

        vectors = embedder.embed_batch(["第一条记忆", "第二条记忆"])

        self.assertEqual(vectors[0], embedder.embed("第一条记忆"))
        self.assertEqual(vectors[1], embedder.embed("第二条记忆"))
        self.assertEqual(embedder.embed_batch([]), [])

    def test_hash_collisions_do_not_produce_a_zero_vector(self):
        vector = HashingEmbedder(1).embed("alpha beta gamma delta")

        self.assertEqual(len(vector), 1)
        self.assertAlmostEqual(abs(vector[0]), 1.0)

    def test_rejects_invalid_configuration_and_text(self):
        with self.assertRaises(EmbeddingError):
            HashingEmbedder(0)
        with self.assertRaises(EmbeddingError):
            HashingEmbedder(True)
        with self.assertRaises(EmbeddingError):
            HashingEmbedder(normalize=1)

        embedder = HashingEmbedder()
        with self.assertRaises(EmbeddingError):
            embedder.embed(" ")
        with self.assertRaises(EmbeddingError):
            embedder.embed("!!!")
        with self.assertRaises(EmbeddingError):
            embedder.embed_batch("not-a-batch")
        with self.assertRaises(EmbeddingError):
            embedder.embed_batch(["valid", 1])


class TestLocalSentenceTransformerEmbedder(unittest.TestCase):
    def test_injected_model_supports_batch_and_normalization(self):
        model = FakeSentenceTransformer()
        embedder = LocalSentenceTransformerEmbedder(model=model)

        vectors = embedder.embed_batch(["alpha", "beta"])

        self.assertEqual(model.calls, [["alpha", "beta"]])
        self.assertEqual(embedder.dimension, 3)
        self.assertEqual(len(vectors), 2)
        for vector in vectors:
            self.assertAlmostEqual(
                math.sqrt(sum(value * value for value in vector)),
                1.0,
            )

    def test_expected_dimension_is_enforced(self):
        embedder = LocalSentenceTransformerEmbedder(
            model=FakeSentenceTransformer(),
            expected_dimension=4,
        )

        with self.assertRaises(EmbeddingError):
            embedder.embed("alpha")

    def test_bad_model_outputs_are_rejected(self):
        wrong_count = LocalSentenceTransformerEmbedder(
            model=BadSentenceTransformer([]),
        )
        with self.assertRaises(EmbeddingError):
            wrong_count.embed("alpha")

        non_finite = LocalSentenceTransformerEmbedder(
            model=BadSentenceTransformer([[1.0, float("inf")]]),
        )
        with self.assertRaises(EmbeddingError):
            non_finite.embed("alpha")

    def test_model_failure_is_wrapped_without_leaking_details(self):
        embedder = LocalSentenceTransformerEmbedder(
            model=FailingSentenceTransformer(),
        )

        with self.assertRaises(EmbeddingError) as context:
            embedder.embed("alpha")

        self.assertNotIn("secret", str(context.exception))
        self.assertIsInstance(context.exception.__cause__, RuntimeError)

    def test_missing_local_path_fails_without_network_access(self):
        embedder = LocalSentenceTransformerEmbedder(
            model_path="definitely-missing-model-directory",
            expected_dimension=3,
        )

        with self.assertRaises(EmbeddingError):
            embedder.embed("alpha")

    def test_model_path_is_loaded_lazily(self):
        with TemporaryDirectory() as directory:
            embedder = LocalSentenceTransformerEmbedder(
                model_path=directory,
                expected_dimension=3,
            )

            self.assertEqual(embedder.dimension, 3)
            self.assertIsNone(embedder._model)


class TestEmbeddingFactory(unittest.TestCase):
    def test_creates_hashing_backend_from_default_or_explicit_config(self):
        self.assertIsInstance(create_embedder(), HashingEmbedder)

        embedder = create_embedder(
            EmbeddingConfig(dimension=32, normalize=False)
        )
        self.assertIsInstance(embedder, HashingEmbedder)
        self.assertEqual(embedder.dimension, 32)
        self.assertFalse(embedder.normalize)

    def test_creates_local_adapter_with_injected_model(self):
        config = EmbeddingConfig(
            provider="sentence_transformer",
            dimension=3,
            model_path="injected-model-does-not-use-this-path",
        )

        embedder = create_embedder(
            config,
            model=FakeSentenceTransformer(),
        )

        self.assertIsInstance(embedder, LocalSentenceTransformerEmbedder)
        self.assertEqual(len(embedder.embed("alpha")), 3)

    def test_rejects_non_config_objects(self):
        with self.assertRaises(EmbeddingError):
            create_embedder({"provider": "hashing"})


if __name__ == "__main__":
    unittest.main()
