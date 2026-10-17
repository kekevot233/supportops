"""只从本地加载或显式注入模型的 CLIP/CLAP 感知编码器。"""

from __future__ import annotations

import os
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from ..core.exceptions import EmbeddingError, MemorySystemError
from ..embeddings.base import normalize_vector, validate_text
from .base import MemoryItem
from .modalities import BaseModalityEncoder, normalize_modality


_OFFLINE_ENVIRONMENT_LOCK = RLock()


class _LocalCrossModalEncoder(BaseModalityEncoder):
    def __init__(
        self,
        *,
        model_path: str | Path | None,
        model: Any | None,
        expected_dimension: int | None,
        normalize: bool,
        max_file_bytes: int,
    ) -> None:
        if model is None and model_path is None:
            raise MemorySystemError("必须提供本地模型路径或已加载模型。")
        if expected_dimension is not None and (
            isinstance(expected_dimension, bool)
            or not isinstance(expected_dimension, int)
            or expected_dimension <= 0
        ):
            raise MemorySystemError("预期向量维度必须是正整数。")
        if not isinstance(normalize, bool):
            raise MemorySystemError("normalize 必须是布尔值。")
        if (
            isinstance(max_file_bytes, bool)
            or not isinstance(max_file_bytes, int)
            or max_file_bytes <= 0
        ):
            raise MemorySystemError("max_file_bytes 必须是正整数。")
        self.model_path = Path(model_path) if model_path is not None else None
        self._model = model
        self._expected_dimension = expected_dimension
        self.normalize = normalize
        self.max_file_bytes = max_file_bytes

    @property
    def dimension(self) -> int:
        if self._expected_dimension is not None:
            return self._expected_dimension
        model = self._ensure_model()
        for attribute in ("dimension", "embedding_dim", "embed_dim"):
            value = getattr(model, attribute, None)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                self._expected_dimension = value
                return value
        getter = getattr(model, "get_sentence_embedding_dimension", None)
        if callable(getter):
            try:
                value = getter()
            except Exception as error:
                raise EmbeddingError("无法读取多模态模型向量维度。") from error
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                self._expected_dimension = value
                return value
        raise MemorySystemError("模型未公开向量维度，请配置 expected_dimension。")

    def _item_path(self, item: MemoryItem, extensions: frozenset[str]) -> Path:
        if not isinstance(item, MemoryItem):
            raise MemorySystemError("模态编码器只能编码 MemoryItem。")
        raw_data = item.metadata.get("raw_data")
        return self._validate_media_path(raw_data, extensions)

    def _query_path(self, query: str, extensions: frozenset[str]) -> Path:
        return self._validate_media_path(query, extensions)

    def _validate_media_path(
        self,
        value: object,
        extensions: frozenset[str],
    ) -> Path:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise MemorySystemError("媒体文件路径不能为空。")
        try:
            path = Path(value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise MemorySystemError("媒体文件不存在或无法访问。") from error
        if not path.is_file():
            raise MemorySystemError("媒体路径必须指向普通文件。")
        if path.suffix.casefold() not in extensions:
            raise MemorySystemError("媒体文件扩展名不受当前编码器支持。")
        try:
            if path.stat().st_size > self.max_file_bytes:
                raise MemorySystemError("媒体文件大小超过编码上限。")
        except OSError as error:
            raise MemorySystemError("无法读取媒体文件信息。") from error
        return path

    def _vector(self, raw: object) -> list[float]:
        if hasattr(raw, "tolist"):
            raw = raw.tolist()
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            if len(raw) == 1 and isinstance(raw[0], Sequence):
                raw = raw[0]
        vector = normalize_vector(
            raw,
            expected_dimension=self._expected_dimension,
            l2_normalize=self.normalize,
        )
        if self._expected_dimension is None:
            self._expected_dimension = len(vector)
        return vector

    def _ensure_model(self) -> Any:
        raise NotImplementedError


class LocalCLIPEncoder(_LocalCrossModalEncoder):
    """将本地图片和文本投影到同一个 CLIP 向量空间。"""

    _EXTENSIONS = frozenset(
        {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
    )

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        model: Any | None = None,
        model_factory: Any | None = None,
        image_loader: Any | None = None,
        expected_dimension: int | None = None,
        normalize: bool = True,
        max_file_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        super().__init__(
            model_path=model_path,
            model=model,
            expected_dimension=expected_dimension,
            normalize=normalize,
            max_file_bytes=max_file_bytes,
        )
        for dependency, name in (
            (model_factory, "model_factory"),
            (image_loader, "image_loader"),
        ):
            if dependency is not None and not callable(dependency):
                raise MemorySystemError(f"{name} 必须可调用。")
        self._model_factory = model_factory
        self._image_loader = image_loader

    @property
    def modality(self) -> str:
        return "image"

    def encode_item(self, item: MemoryItem) -> list[float]:
        return self._encode_image(self._item_path(item, self._EXTENSIONS))

    def encode_query(self, query: str, *, query_modality: str = "text") -> list[float]:
        source = normalize_modality(query_modality)
        if source == "text":
            return self._encode([validate_text(query)], "CLIP 文本查询编码失败。")
        if source == "image":
            return self._encode_image(self._query_path(query, self._EXTENSIONS))
        raise MemorySystemError("CLIP 编码器只支持文本或图像查询。")

    def _encode_image(self, path: Path) -> list[float]:
        loader = self._image_loader or self._default_image_loader
        try:
            image = loader(path)
        except Exception as error:
            raise MemorySystemError("图像文件解码失败。") from error
        return self._encode([image], "CLIP 图像编码失败。")

    def _encode(self, inputs: list[object], message: str) -> list[float]:
        try:
            raw = self._ensure_model().encode(inputs)
            return self._vector(raw)
        except (EmbeddingError, MemorySystemError):
            raise
        except Exception as error:
            raise EmbeddingError(message) from error

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self.model_path is None or not self.model_path.exists():
            raise MemorySystemError("本地 CLIP 模型路径不存在。")
        try:
            if self._model_factory is not None:
                self._model = self._model_factory(self.model_path)
            else:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(
                    str(self.model_path), local_files_only=True
                )
        except ImportError as error:
            raise MemorySystemError(
                "CLIP 本地加载需要安装 sentence-transformers 和 Pillow。"
            ) from error
        except Exception as error:
            raise MemorySystemError("本地 CLIP 模型加载失败。") from error
        return self._model

    @staticmethod
    def _default_image_loader(path: Path) -> Any:
        try:
            from PIL import Image
        except ImportError as error:
            raise MemorySystemError("图像解码需要安装 Pillow。") from error
        with Image.open(path) as image:
            return image.convert("RGB").copy()


class LocalCLAPEncoder(_LocalCrossModalEncoder):
    """将本地音频和文本投影到同一个 CLAP 向量空间。"""

    _EXTENSIONS = frozenset(
        {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
    )

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        model: Any | None = None,
        model_factory: Any | None = None,
        expected_dimension: int | None = None,
        normalize: bool = True,
        max_file_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        super().__init__(
            model_path=model_path,
            model=model,
            expected_dimension=expected_dimension,
            normalize=normalize,
            max_file_bytes=max_file_bytes,
        )
        if model_factory is not None and not callable(model_factory):
            raise MemorySystemError("model_factory 必须可调用。")
        self._model_factory = model_factory

    @property
    def modality(self) -> str:
        return "audio"

    def encode_item(self, item: MemoryItem) -> list[float]:
        return self._encode_audio(self._item_path(item, self._EXTENSIONS))

    def encode_query(self, query: str, *, query_modality: str = "text") -> list[float]:
        source = normalize_modality(query_modality)
        if source == "text":
            return self._encode_text(validate_text(query))
        if source == "audio":
            return self._encode_audio(self._query_path(query, self._EXTENSIONS))
        raise MemorySystemError("CLAP 编码器只支持文本或音频查询。")

    def _encode_audio(self, path: Path) -> list[float]:
        model = self._ensure_model()
        try:
            method = getattr(model, "encode_audio", None)
            raw = (
                method([str(path)])
                if callable(method)
                else model.get_audio_embedding_from_filelist(
                    x=[str(path)], use_tensor=False
                )
            )
            return self._vector(raw)
        except (EmbeddingError, MemorySystemError):
            raise
        except Exception as error:
            raise EmbeddingError("CLAP 音频编码失败。") from error

    def _encode_text(self, text: str) -> list[float]:
        model = self._ensure_model()
        try:
            method = getattr(model, "encode_text", None)
            raw = (
                method([text])
                if callable(method)
                else model.get_text_embedding([text], use_tensor=False)
            )
            return self._vector(raw)
        except (EmbeddingError, MemorySystemError):
            raise
        except Exception as error:
            raise EmbeddingError("CLAP 文本编码失败。") from error

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        if self.model_path is None or not self.model_path.is_file():
            raise MemorySystemError("本地 CLAP checkpoint 路径不存在。")
        try:
            if self._model_factory is not None:
                self._model = self._model_factory(self.model_path)
            else:
                with _offline_model_environment():
                    import laion_clap

                    model = laion_clap.CLAP_Module(enable_fusion=False)
                    model.load_ckpt(ckpt=str(self.model_path))
                    self._model = model
        except ImportError as error:
            raise MemorySystemError("CLAP 本地加载需要安装 laion-clap。") from error
        except Exception as error:
            raise MemorySystemError("本地 CLAP 模型加载失败。") from error
        return self._model


@contextmanager
def _offline_model_environment():
    """临时禁止模型库访问网络，并完整恢复调用方环境。"""
    names = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    with _OFFLINE_ENVIRONMENT_LOCK:
        previous = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                os.environ[name] = "1"
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


__all__ = ["LocalCLAPEncoder", "LocalCLIPEncoder"]
