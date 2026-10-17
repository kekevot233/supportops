"""安全、有限且依赖可注入的本地文档加载器。"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ..core.exceptions import RAGError
from .models import Document


class LocalTextDocumentLoader:
    def __init__(
        self,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        allowed_extensions: tuple[str, ...] = (".txt", ".md", ".markdown"),
    ) -> None:
        _validate_max_bytes(max_bytes)
        if not isinstance(allowed_extensions, tuple) or not allowed_extensions:
            raise RAGError("allowed_extensions 必须是非空元组。")
        normalized = []
        for extension in allowed_extensions:
            if not isinstance(extension, str) or not extension.startswith("."):
                raise RAGError("文档扩展名必须以点号开头。")
            normalized.append(extension.casefold())
        self.max_bytes = max_bytes
        self.allowed_extensions = tuple(dict.fromkeys(normalized))

    def load(
        self,
        path: str | Path,
        *,
        document_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Document:
        resolved = _resolve_file(path, self.allowed_extensions, self.max_bytes)
        try:
            content = resolved.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as error:
            raise RAGError("文档必须使用 UTF-8 编码。") from error
        except OSError as error:
            raise RAGError("本地文档读取失败。") from error
        if not content.strip():
            raise RAGError("文档内容不能为空。")
        return _build_document(
            resolved,
            content,
            document_id=document_id,
            title=title,
            metadata=metadata,
        )


class PDFDocumentLoader:
    """使用可选 pypdf 或注入的读取器提取逐页文本。"""

    def __init__(
        self,
        *,
        max_bytes: int = 50 * 1024 * 1024,
        reader_factory: Any | None = None,
    ) -> None:
        _validate_max_bytes(max_bytes)
        if reader_factory is not None and not callable(reader_factory):
            raise RAGError("reader_factory 必须可调用。")
        self.max_bytes = max_bytes
        self._reader_factory = reader_factory

    def load(
        self,
        path: str | Path,
        *,
        document_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        password: str | None = None,
    ) -> Document:
        resolved = _resolve_file(path, (".pdf",), self.max_bytes)
        if password is not None and (not isinstance(password, str) or not password):
            raise RAGError("PDF 密码必须是非空字符串或 None。")
        factory = self._reader_factory or self._default_reader_factory()
        try:
            reader = factory(resolved)
            if bool(getattr(reader, "is_encrypted", False)):
                if password is None or not reader.decrypt(password):
                    raise RAGError("PDF 已加密且未提供有效密码。")
            pages = list(reader.pages)
        except RAGError:
            raise
        except Exception as error:
            raise RAGError("PDF 文档打开失败。") from error
        parts: list[str] = []
        spans: list[dict[str, int]] = []
        cursor = 0
        for page_number, page in enumerate(pages, 1):
            try:
                page_text = page.extract_text() or ""
            except Exception as error:
                raise RAGError(f"PDF 第 {page_number} 页文本提取失败。") from error
            normalized = page_text.strip()
            if not normalized:
                continue
            if parts:
                cursor += 2
            start = cursor
            parts.append(normalized)
            cursor += len(normalized)
            spans.append(
                {
                    "page_number": page_number,
                    "start_char": start,
                    "end_char": cursor,
                }
            )
        if not parts:
            raise RAGError("PDF 没有可提取文本，可能需要 OCR。")
        combined_metadata = {
            **_copy_metadata(metadata),
            "source_format": "pdf",
            "_page_spans": spans,
        }
        return _build_document(
            resolved,
            "\n\n".join(parts),
            document_id=document_id,
            title=title,
            metadata=combined_metadata,
        )

    @staticmethod
    def _default_reader_factory() -> Any:
        try:
            from pypdf import PdfReader
        except ImportError as error:
            raise RAGError(
                "PDF 加载需要安装可选依赖：pip install \".[document-loaders]\"。"
            ) from error
        return PdfReader


class WordDocumentLoader:
    """使用可选 python-docx 或注入的工厂提取 DOCX 标题、正文和表格。"""

    def __init__(
        self,
        *,
        max_bytes: int = 50 * 1024 * 1024,
        document_factory: Any | None = None,
    ) -> None:
        _validate_max_bytes(max_bytes)
        if document_factory is not None and not callable(document_factory):
            raise RAGError("document_factory 必须可调用。")
        self.max_bytes = max_bytes
        self._document_factory = document_factory

    def load(
        self,
        path: str | Path,
        *,
        document_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Document:
        resolved = _resolve_file(path, (".docx",), self.max_bytes)
        factory = self._document_factory or self._default_document_factory()
        try:
            document = factory(resolved)
            blocks = self._extract_blocks(document)
        except RAGError:
            raise
        except Exception as error:
            raise RAGError("DOCX 文档解析失败。") from error
        content = "\n\n".join(block for block in blocks if block.strip())
        if not content.strip():
            raise RAGError("DOCX 文档没有可提取文本。")
        core_title = getattr(getattr(document, "core_properties", None), "title", None)
        combined_metadata = {**_copy_metadata(metadata), "source_format": "docx"}
        return _build_document(
            resolved,
            content,
            document_id=document_id,
            title=title or (core_title.strip() if isinstance(core_title, str) else None),
            metadata=combined_metadata,
        )

    @classmethod
    def _extract_blocks(cls, document: Any) -> list[str]:
        if hasattr(getattr(document, "element", None), "body"):
            try:
                from docx.table import Table
                from docx.text.paragraph import Paragraph

                blocks = []
                for child in document.element.body.iterchildren():
                    tag = str(getattr(child, "tag", ""))
                    if tag.endswith("}p"):
                        rendered = cls._paragraph_to_text(Paragraph(child, document))
                    elif tag.endswith("}tbl"):
                        rendered = cls._table_to_markdown(Table(child, document))
                    else:
                        continue
                    if rendered:
                        blocks.append(rendered)
                return blocks
            except ImportError as error:
                raise RAGError("DOCX 解析组件不完整。") from error
        blocks = []
        for paragraph in getattr(document, "paragraphs", []):
            rendered = cls._paragraph_to_text(paragraph)
            if rendered:
                blocks.append(rendered)
        for table in getattr(document, "tables", []):
            markdown = cls._table_to_markdown(table)
            if markdown:
                blocks.append(markdown)
        return blocks

    @staticmethod
    def _paragraph_to_text(paragraph: Any) -> str:
        text = str(getattr(paragraph, "text", "")).strip()
        if not text:
            return ""
        style_name = str(getattr(getattr(paragraph, "style", None), "name", ""))
        match = re.fullmatch(r"Heading\s+([1-6])", style_name, re.IGNORECASE)
        return f"{'#' * int(match.group(1))} {text}" if match else text

    @staticmethod
    def _table_to_markdown(table: Any) -> str:
        rows = []
        for row in getattr(table, "rows", []):
            cells = [
                str(getattr(cell, "text", "")).strip().replace("|", "\\|")
                for cell in getattr(row, "cells", [])
            ]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        normalized = [row + [""] * (width - len(row)) for row in rows]
        header = normalized[0]
        lines = ["| " + " | ".join(header) + " |"]
        lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
        lines.extend("| " + " | ".join(row) + " |" for row in normalized[1:])
        return "\n".join(lines)

    @staticmethod
    def _default_document_factory() -> Any:
        try:
            from docx import Document as DocxDocument
        except ImportError as error:
            raise RAGError(
                "DOCX 加载需要安装可选依赖：pip install \".[document-loaders]\"。"
            ) from error
        return DocxDocument


def _validate_max_bytes(max_bytes: object) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise RAGError("max_bytes 必须是正整数。")
    return max_bytes


def _resolve_file(
    path: str | Path,
    allowed_extensions: tuple[str, ...],
    max_bytes: int,
) -> Path:
    if not isinstance(path, (str, Path)):
        raise RAGError("文档路径必须是字符串或 Path。")
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RAGError("本地文档不存在或无法访问。") from error
    if not resolved.is_file():
        raise RAGError("文档路径必须指向普通文件。")
    if resolved.suffix.casefold() not in allowed_extensions:
        raise RAGError("当前加载器不支持该文档格式。")
    try:
        if resolved.stat().st_size > max_bytes:
            raise RAGError("文档大小超过加载上限。")
    except OSError as error:
        raise RAGError("无法读取文档文件信息。") from error
    return resolved


def _build_document(
    resolved: Path,
    content: str,
    *,
    document_id: str | None,
    title: str | None,
    metadata: dict[str, Any] | None,
) -> Document:
    source = str(resolved)
    actual_id = document_id or str(
        uuid5(NAMESPACE_URL, f"hello-agents-document:{source.casefold()}")
    )
    try:
        return Document(
            id=actual_id,
            content=content,
            source=source,
            title=title or resolved.stem,
            metadata=_copy_metadata(metadata),
        )
    except ValueError as error:
        raise RAGError("文档内容或元数据无效。") from error


def _copy_metadata(metadata: object) -> dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise RAGError("文档元数据必须是字典。")
    return dict(metadata)


__all__ = [
    "LocalTextDocumentLoader",
    "PDFDocumentLoader",
    "WordDocumentLoader",
]
