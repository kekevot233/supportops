"""保留 Markdown 章节信息的确定性字符切块器。"""

from __future__ import annotations

import hashlib
import re
from uuid import NAMESPACE_URL, uuid5

from ..core.exceptions import RAGError
from .models import Document, DocumentChunk


_HEADING_PATTERN = re.compile(r"(?m)^#{1,6}[ \t]+(.+?)[ \t]*$")
_BREAK_CHARACTERS = "\n。！？；.!?; "


class DocumentChunker:
    def __init__(
        self,
        *,
        chunk_size: int = 1000,
        chunk_overlap: int = 120,
        min_chunk_chars: int = 40,
    ) -> None:
        for value, name in (
            (chunk_size, "chunk_size"),
            (chunk_overlap, "chunk_overlap"),
            (min_chunk_chars, "min_chunk_chars"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RAGError(f"{name} 必须是非负整数。")
        if chunk_size <= 0:
            raise RAGError("chunk_size 必须大于零。")
        if chunk_overlap >= chunk_size:
            raise RAGError("chunk_overlap 必须小于 chunk_size。")
        if min_chunk_chars <= 0 or min_chunk_chars > chunk_size:
            raise RAGError("min_chunk_chars 必须在 1 到 chunk_size 之间。")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_chars = min_chunk_chars

    def chunk(self, document: Document) -> list[DocumentChunk]:
        if not isinstance(document, Document):
            raise RAGError("chunk() 只能接收 Document。")
        text = document.content
        headings = [
            (match.start(), match.group(1).strip())
            for match in _HEADING_PATTERN.finditer(text)
        ]
        page_spans = document.metadata.get("_page_spans", [])
        chunk_metadata = dict(document.metadata)
        chunk_metadata.pop("_page_spans", None)
        version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        chunks: list[DocumentChunk] = []
        start = 0
        while start < len(text):
            hard_end = min(len(text), start + self.chunk_size)
            end = self._find_break(text, start, hard_end)
            raw = text[start:end]
            stripped = raw.strip()
            if stripped:
                leading = len(raw) - len(raw.lstrip())
                trailing = len(raw) - len(raw.rstrip())
                actual_start = start + leading
                actual_end = end - trailing
                index = len(chunks)
                chunk_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        ":".join(
                            [
                                "hello-agents-chunk",
                                document.id,
                                version,
                                str(index),
                                str(actual_start),
                            ]
                        ),
                    )
                )
                chunks.append(
                    DocumentChunk(
                        id=chunk_id,
                        document_id=document.id,
                        content=stripped,
                        source=document.source,
                        title=document.title,
                        chunk_index=index,
                        start_char=actual_start,
                        end_char=actual_end,
                        section=self._section_at(headings, actual_start),
                        page_number=(
                            document.metadata.get("page_number")
                            or self._page_at(page_spans, actual_start)
                        ),
                        metadata={**chunk_metadata, "document_version": version},
                    )
                )
            if end >= len(text):
                break
            next_start = max(start + 1, end - self.chunk_overlap)
            start = next_start
        if not chunks:
            raise RAGError("文档切块后没有可摄取内容。")
        return chunks

    def _find_break(self, text: str, start: int, hard_end: int) -> int:
        if hard_end >= len(text):
            return len(text)
        earliest = start + max(self.min_chunk_chars, int(self.chunk_size * 0.6))
        for position in range(hard_end, earliest - 1, -1):
            if text[position - 1] in _BREAK_CHARACTERS:
                return position
        return hard_end

    @staticmethod
    def _section_at(headings: list[tuple[int, str]], position: int) -> str | None:
        section = None
        for heading_position, heading in headings:
            if heading_position > position:
                break
            section = heading
        return section

    @staticmethod
    def _page_at(page_spans: object, position: int) -> int | None:
        if not isinstance(page_spans, list):
            raise RAGError("文档页码区间元数据必须是列表。")
        for span in page_spans:
            if not isinstance(span, dict):
                raise RAGError("文档页码区间项必须是字典。")
            try:
                page = span["page_number"]
                start = span["start_char"]
                end = span["end_char"]
            except KeyError as error:
                raise RAGError("文档页码区间缺少必要字段。") from error
            if not all(isinstance(value, int) and not isinstance(value, bool) for value in (page, start, end)):
                raise RAGError("文档页码区间字段必须是整数。")
            if start <= position < end:
                return page
        return None


__all__ = ["DocumentChunker"]
