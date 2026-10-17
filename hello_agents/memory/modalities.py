"""感知记忆使用的模态识别与编码器抽象。"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path

from ..core.exceptions import MemorySystemError
from ..embeddings import BaseEmbedder
from .base import MemoryItem


_MODALITY_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")
_IMAGE_EXTENSIONS = frozenset(
    {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)
_AUDIO_EXTENSIONS = frozenset(
    {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
)
_VIDEO_EXTENSIONS = frozenset(
    {".avi", ".mkv", ".mov", ".mp4", ".mpeg", ".webm"}
)
_TEXT_EXTENSIONS = frozenset(
    {".csv", ".doc", ".docx", ".json", ".md", ".pdf", ".rtf", ".txt"}
)


def normalize_modality(value: object) -> str:
    """把模态名规范为适合配置键和 payload 的小写标识符。"""
    if not isinstance(value, str) or not value.strip():
        raise MemorySystemError("模态名称不能为空。")
    normalized = value.strip().lower()
    if not _MODALITY_PATTERN.fullmatch(normalized):
        raise MemorySystemError("模态名称只能包含小写字母、数字、下划线和连字符。")
    return normalized


def infer_modality(
    file_path: str | Path | None = None,
    *,
    explicit: str | None = None,
) -> str:
    """优先采用显式模态，否则仅根据扩展名推断，不读取文件内容。"""
    if explicit is not None:
        return normalize_modality(explicit)
    if file_path is None:
        return "text"
    if isinstance(file_path, Path):
        path_text = str(file_path)
    elif isinstance(file_path, str):
        path_text = file_path.strip()
    else:
        raise MemorySystemError("感知文件路径必须是字符串或 Path。")
    if not path_text:
        raise MemorySystemError("感知文件路径不能为空。")
    extension = Path(path_text).suffix.lower()
    if extension in _IMAGE_EXTENSIONS:
        return "image"
    if extension in _AUDIO_EXTENSIONS:
        return "audio"
    if extension in _VIDEO_EXTENSIONS:
        return "video"
    if extension in _TEXT_EXTENSIONS:
        return "text"
    return "file"


class BaseModalityEncoder(ABC):
    """把某类感知对象及查询映射到该模态的向量空间。"""

    @property
    @abstractmethod
    def modality(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def dimension(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def encode_item(self, item: MemoryItem) -> list[float]:
        """编码待保存的感知记忆。"""
        raise NotImplementedError

    @abstractmethod
    def encode_query(
        self,
        query: str,
        *,
        query_modality: str = "text",
    ) -> list[float]:
        """把查询投影到当前编码器对应的目标向量空间。"""
        raise NotImplementedError


class TextModalityEncoder(BaseModalityEncoder):
    """使用现有文本 Embedding 编码文本感知记忆。"""

    def __init__(self, embedder: BaseEmbedder) -> None:
        if not isinstance(embedder, BaseEmbedder):
            raise MemorySystemError("文本模态编码器需要 BaseEmbedder。")
        self.embedder = embedder

    @property
    def modality(self) -> str:
        return "text"

    @property
    def dimension(self) -> int:
        return self.embedder.dimension

    def encode_item(self, item: MemoryItem) -> list[float]:
        if not isinstance(item, MemoryItem):
            raise MemorySystemError("模态编码器只能编码 MemoryItem。")
        return self.embedder.embed(item.content)

    def encode_query(
        self,
        query: str,
        *,
        query_modality: str = "text",
    ) -> list[float]:
        if normalize_modality(query_modality) != "text":
            raise MemorySystemError("文本编码器只支持文本查询。")
        return self.embedder.embed(query)


class MetadataModalityEncoder(BaseModalityEncoder):
    """只编码描述文字和文件名的离线基线，不读取媒体文件内容。"""

    def __init__(self, modality: str, embedder: BaseEmbedder) -> None:
        normalized = normalize_modality(modality)
        if normalized == "text":
            raise MemorySystemError("文本模态请使用 TextModalityEncoder。")
        if not isinstance(embedder, BaseEmbedder):
            raise MemorySystemError("元数据模态编码器需要 BaseEmbedder。")
        self._modality = normalized
        self.embedder = embedder

    @property
    def modality(self) -> str:
        return self._modality

    @property
    def dimension(self) -> int:
        return self.embedder.dimension

    def encode_item(self, item: MemoryItem) -> list[float]:
        if not isinstance(item, MemoryItem):
            raise MemorySystemError("模态编码器只能编码 MemoryItem。")
        raw_data = item.metadata.get("raw_data")
        if not isinstance(raw_data, str) or not raw_data.strip():
            raise MemorySystemError(
                f"{self.modality} 感知记忆必须提供 raw_data 文件路径。"
            )
        descriptor = f"{item.content} {Path(raw_data.strip()).name}".strip()
        return self.embedder.embed(descriptor)

    def encode_query(
        self,
        query: str,
        *,
        query_modality: str = "text",
    ) -> list[float]:
        normalize_modality(query_modality)
        return self.embedder.embed(query)


__all__ = [
    "BaseModalityEncoder",
    "MetadataModalityEncoder",
    "TextModalityEncoder",
    "infer_modality",
    "normalize_modality",
]
