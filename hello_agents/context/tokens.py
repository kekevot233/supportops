"""离线 Token 计数契约及已加载 tokenizer 适配器。"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from numbers import Integral
from typing import Any

from ..core.exceptions import ContextEngineeringError


class BaseTokenCounter(ABC):
    """只计算传入文本；聊天模板与其他 API 消息开销由应用另行预留。"""

    name = "custom"

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        raise NotImplementedError


class HeuristicTokenCounter(BaseTokenCounter):
    """保留现有中英文估算行为，无模型或词表依赖。"""

    name = "heuristic"

    def count_tokens(self, text: str) -> int:
        if not isinstance(text, str):
            raise ContextEngineeringError("Token 计数输入必须是字符串。")
        if not text:
            return 0
        chinese = sum("\u4e00" <= char <= "\u9fff" for char in text)
        other = sum(
            not char.isspace() and not ("\u4e00" <= char <= "\u9fff")
            for char in text
        )
        return max(1, chinese + math.ceil(other / 4))


class TokenizerTokenCounter(BaseTokenCounter):
    """适配 encode(text, **kwargs) -> 一维 token ID 序列，不负责加载或下载。"""

    def __init__(
        self,
        tokenizer: Any,
        *,
        name: str,
        encode_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        if not callable(getattr(tokenizer, "encode", None)):
            raise ContextEngineeringError("tokenizer 必须提供 encode()。")
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.strip()) > 100
            or any(char in name for char in "\r\n\x00")
        ):
            raise ContextEngineeringError("Token 计数器名称无效。")
        if encode_kwargs is not None and not isinstance(encode_kwargs, Mapping):
            raise ContextEngineeringError("encode_kwargs 必须是映射或 None。")
        self.name = name.strip()
        self._tokenizer = tokenizer
        self._encode_kwargs = dict(encode_kwargs or {})

    def count_tokens(self, text: str) -> int:
        if not isinstance(text, str):
            raise ContextEngineeringError("Token 计数输入必须是字符串。")
        if not text:
            return 0
        try:
            ids = self._tokenizer.encode(text, **self._encode_kwargs)
            if (
                not isinstance(ids, Sequence)
                or isinstance(ids, (str, bytes, bytearray))
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, Integral)
                    or value < 0
                    for value in ids
                )
                or (text.strip() and not ids)
            ):
                raise ValueError("invalid token IDs")
            return len(ids)
        except Exception as error:
            raise ContextEngineeringError("Tokenizer 编码失败或返回无效 Token ID。") from error


__all__ = ["BaseTokenCounter", "HeuristicTokenCounter", "TokenizerTokenCounter"]
