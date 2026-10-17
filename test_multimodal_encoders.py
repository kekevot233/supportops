import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hello_agents import (
    EmbeddingError,
    InMemoryVectorStore,
    LocalCLAPEncoder,
    LocalCLIPEncoder,
    MemoryItem,
    MemorySystemError,
    PerceptualMemory,
    SQLiteMemoryStore,
)


def perception(memory_id, path, modality):
    return MemoryItem(
        id=memory_id,
        content=f"{modality} 感知内容",
        memory_type="perceptual",
        metadata={"raw_data": str(path), "modality": modality},
    )


class FakeCLIPModel:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    def get_sentence_embedding_dimension(self):
        return 4

    def encode(self, inputs):
        self.calls.append(inputs)
        if self.error is not None:
            raise self.error
        value = inputs[0]
        marker = str(value).casefold()
        if "cat" in marker or "猫" in marker:
            return [[1.0, 0.0, 0.0, 0.0]]
        return [[0.0, 1.0, 0.0, 0.0]]


class FakeCLAPModel:
    embedding_dim = 4

    def __init__(self, *, error=None):
        self.error = error
        self.audio_calls = []
        self.text_calls = []

    def encode_audio(self, paths):
        self.audio_calls.append(paths)
        if self.error is not None:
            raise self.error
        marker = paths[0].casefold()
        return [[1.0, 0.0, 0.0, 0.0]] if "rain" in marker else [[0.0, 1.0, 0.0, 0.0]]

    def encode_text(self, texts):
        self.text_calls.append(texts)
        if self.error is not None:
            raise self.error
        return [[1.0, 0.0, 0.0, 0.0]] if "雨" in texts[0] else [[0.0, 1.0, 0.0, 0.0]]


class TestLocalCLIPEncoder(unittest.TestCase):
    def test_encodes_image_and_text_in_shared_space(self):
        model = FakeCLIPModel()
        loaded = []
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "cat.png"
            image.write_bytes(b"fake-image")
            encoder = LocalCLIPEncoder(
                model=model,
                image_loader=lambda path: loaded.append(path) or f"image:{path.stem}",
            )

            image_vector = encoder.encode_item(perception("cat", image, "image"))
            text_vector = encoder.encode_query("一只猫")
            path_vector = encoder.encode_query(str(image), query_modality="image")

        self.assertEqual(encoder.dimension, 4)
        self.assertEqual(image_vector, text_vector)
        self.assertEqual(image_vector, path_vector)
        self.assertEqual(len(loaded), 2)

    def test_lazy_local_factory_never_uses_model_name(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "clip-model"
            model_path.mkdir()
            encoder = LocalCLIPEncoder(
                model_path,
                model_factory=lambda path: calls.append(path) or FakeCLIPModel(),
                image_loader=lambda path: f"image:{path.stem}",
                expected_dimension=4,
            )
            self.assertEqual(calls, [])
            encoder.encode_query("猫")

        self.assertEqual(calls, [model_path])

    def test_rejects_missing_bad_extension_size_and_query_modality(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = root / "not-image.txt"
            text.write_bytes(b"data")
            large = root / "large.png"
            large.write_bytes(b"12345")
            encoder = LocalCLIPEncoder(
                model=FakeCLIPModel(),
                image_loader=lambda path: path,
                max_file_bytes=4,
            )
            for path in (root / "missing.png", text, large):
                with self.subTest(path=path), self.assertRaises(MemorySystemError):
                    encoder.encode_item(perception("bad", path, "image"))
            with self.assertRaises(MemorySystemError):
                encoder.encode_query("sound", query_modality="audio")

    def test_model_and_decoder_errors_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "cat.png"
            image.write_bytes(b"fake")
            decoder = LocalCLIPEncoder(
                model=FakeCLIPModel(),
                image_loader=lambda _: (_ for _ in ()).throw(RuntimeError("secret")),
            )
            with self.assertRaises(MemorySystemError) as caught:
                decoder.encode_item(perception("cat", image, "image"))
            self.assertNotIn("secret", str(caught.exception))

            model_error = LocalCLIPEncoder(
                model=FakeCLIPModel(error=RuntimeError("secret")),
                image_loader=lambda path: path,
            )
            with self.assertRaises(EmbeddingError) as caught:
                model_error.encode_query("猫")
            self.assertNotIn("secret", str(caught.exception))


class TestLocalCLAPEncoder(unittest.TestCase):
    def test_encodes_audio_and_text_in_shared_space(self):
        model = FakeCLAPModel()
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "rain.wav"
            audio.write_bytes(b"fake-audio")
            encoder = LocalCLAPEncoder(model=model)

            audio_vector = encoder.encode_item(perception("rain", audio, "audio"))
            text_vector = encoder.encode_query("下雨的声音")
            path_vector = encoder.encode_query(str(audio), query_modality="audio")

        self.assertEqual(encoder.dimension, 4)
        self.assertEqual(audio_vector, text_vector)
        self.assertEqual(audio_vector, path_vector)
        self.assertEqual(len(model.audio_calls), 2)

    def test_local_checkpoint_factory_is_lazy(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "clap.pt"
            checkpoint.write_bytes(b"fake-model")
            encoder = LocalCLAPEncoder(
                checkpoint,
                model_factory=lambda path: calls.append(path) or FakeCLAPModel(),
                expected_dimension=4,
            )
            self.assertEqual(calls, [])
            encoder.encode_query("雨声")

        self.assertEqual(calls, [checkpoint])

    def test_default_loader_forces_offline_mode_and_restores_environment(self):
        observed = {}

        class DefaultFakeModel(FakeCLAPModel):
            def load_ckpt(self, *, ckpt):
                observed["checkpoint"] = ckpt

        def make_model(*, enable_fusion):
            observed["enable_fusion"] = enable_fusion
            observed["hf_offline"] = os.environ.get("HF_HUB_OFFLINE")
            observed["transformers_offline"] = os.environ.get(
                "TRANSFORMERS_OFFLINE"
            )
            return DefaultFakeModel()

        fake_module = types.SimpleNamespace(CLAP_Module=make_model)
        previous_transformers = os.environ.get("TRANSFORMERS_OFFLINE")
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "clap.pt"
            checkpoint.write_bytes(b"fake-model")
            with patch.dict(sys.modules, {"laion_clap": fake_module}), patch.dict(
                os.environ, {"HF_HUB_OFFLINE": "before"}, clear=False
            ):
                encoder = LocalCLAPEncoder(checkpoint, expected_dimension=4)
                encoder.encode_query("雨声")
                self.assertEqual(os.environ["HF_HUB_OFFLINE"], "before")
                self.assertEqual(
                    os.environ.get("TRANSFORMERS_OFFLINE"),
                    previous_transformers,
                )

        self.assertEqual(observed["hf_offline"], "1")
        self.assertEqual(observed["transformers_offline"], "1")
        self.assertFalse(observed["enable_fusion"])
        self.assertEqual(observed["checkpoint"], str(checkpoint))

    def test_rejects_unsupported_query_and_wraps_model_error(self):
        encoder = LocalCLAPEncoder(
            model=FakeCLAPModel(error=RuntimeError("secret"))
        )
        with self.assertRaises(MemorySystemError):
            encoder.encode_query("image", query_modality="image")
        with self.assertRaises(EmbeddingError) as caught:
            encoder.encode_query("雨声")
        self.assertNotIn("secret", str(caught.exception))


class TestMultimodalPerceptualMemory(unittest.TestCase):
    def test_text_queries_retrieve_real_media_encodings(self):
        store = SQLiteMemoryStore(":memory:")
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cat = root / "cat.png"
                dog = root / "dog.png"
                rain = root / "rain.wav"
                for path in (cat, dog, rain):
                    path.write_bytes(b"fake")
                clip = LocalCLIPEncoder(
                    model=FakeCLIPModel(),
                    image_loader=lambda path: f"image:{path.stem}",
                )
                clap = LocalCLAPEncoder(model=FakeCLAPModel())
                memory = PerceptualMemory(
                    document_store=store,
                    encoders={"image": clip, "audio": clap},
                    vector_stores={
                        "image": InMemoryVectorStore(4),
                        "audio": InMemoryVectorStore(4),
                    },
                    user_id="alice",
                )
                memory.add(perception("cat", cat, "image"))
                memory.add(perception("dog", dog, "image"))
                memory.add(perception("rain", rain, "audio"))

                image_results = memory.retrieve(
                    "猫", limit=1, target_modality="image"
                )
                audio_results = memory.retrieve(
                    "雨声", limit=1, target_modality="audio"
                )

            self.assertEqual(image_results[0].item.id, "cat")
            self.assertEqual(audio_results[0].item.id, "rain")
        finally:
            store.close()

    def test_configuration_validation_and_bad_vectors(self):
        with self.assertRaises(MemorySystemError):
            LocalCLIPEncoder()
        with self.assertRaises(MemorySystemError):
            LocalCLAPEncoder(model=FakeCLAPModel(), expected_dimension=True)

        class BadModel(FakeCLIPModel):
            def encode(self, inputs):
                return [[1.0, float("nan"), 0.0, 0.0]]

        with self.assertRaises(EmbeddingError):
            LocalCLIPEncoder(model=BadModel()).encode_query("猫")


if __name__ == "__main__":
    unittest.main()
