"""只摄取显式清单中的项目演示资料，不扫描工作区。"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hello_agents import (
    Document, DocumentChunker, DocumentKnowledgeBase, HashingEmbedder,
    InMemoryVectorStore, LocalTextDocumentLoader, SQLiteMemoryStore,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data" / "supportops"


def validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value or value != value.strip() or "\\" in value or ":" in value
        or path.is_absolute() or ".." in path.parts
        or any(part.startswith(".") for part in path.parts)
        or str(path) != value
    ):
        raise ValueError("资料路径必须是显式的普通仓库相对路径。")
    return value


class SeedDocumentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^kb-[a-z0-9-]{1,60}$")
    path: str
    title: str = Field(min_length=1, max_length=200)
    source_refs: list[str] = Field(min_length=1, max_length=20)

    @field_validator("path")
    @classmethod
    def validate_document_path(cls, value: str) -> str:
        validate_relative_path(value)
        if PurePosixPath(value).suffix != ".md":
            raise ValueError("种子资料只允许 Markdown 文件。")
        return value

    @field_validator("source_refs")
    @classmethod
    def validate_references(cls, values: list[str]) -> list[str]:
        for value in values:
            validate_relative_path(value)
            if PurePosixPath(value).suffix not in {".py", ".md", ".toml"}:
                raise ValueError("来源只记录项目文档、代码或项目配置路径。")
        return values


class SeedManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    corpus_id: str = Field(min_length=1)
    as_of: date
    origin: Literal["curated_project_documentation", "synthetic_support_fixture"]
    documents: list[SeedDocumentSpec] = Field(min_length=1, max_length=100)


@dataclass(frozen=True)
class SeedCorpus:
    manifest: SeedManifest
    documents: tuple[Document, ...]
    sha256: str


def load_seed_corpus(corpus_dir: Path | None = None, *, source_prefix="data/supportops/corpus") -> SeedCorpus:
    validate_relative_path(source_prefix)
    root = (corpus_dir or DEFAULT_DATA_ROOT / "corpus").resolve()
    manifest = SeedManifest.model_validate_json(read_small_text(root / "manifest.json"))
    ids = [entry.id for entry in manifest.documents]
    paths = [entry.path for entry in manifest.documents]
    if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
        raise ValueError("摄取清单的文档 ID 和路径必须唯一。")
    loader = LocalTextDocumentLoader(max_bytes=1_000_000, allowed_extensions=(".md",))
    documents = []
    for entry in manifest.documents:
        path = (root / entry.path).resolve()
        if not path.is_relative_to(root):
            raise ValueError("资料路径不能逃逸 corpus 目录。")
        document = loader.load(
            path, document_id=entry.id, title=entry.title,
            metadata={
                "corpus_id": manifest.corpus_id, "as_of": manifest.as_of.isoformat(),
                "origin": manifest.origin, "source_refs": list(entry.source_refs),
            },
        )
        # 不把本机绝对路径作为可展示引用。
        document = document.model_copy(update={"source": f"{source_prefix}/{entry.path}"})
        documents.append(document)
    canonical = json.dumps(
        {"manifest": manifest.model_dump(mode="json"),
         "documents": [{"id": doc.id, "content": doc.content} for doc in documents]},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    return SeedCorpus(manifest, tuple(documents), hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def read_small_text(path: Path) -> str:
    if not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError("资料必须是 2 MB 以内的普通文件。")
    return path.read_text(encoding="utf-8-sig")


@contextmanager
def open_seed_index(corpus: SeedCorpus) -> Iterator[DocumentKnowledgeBase]:
    """一次进程内构建演示索引，结束时关闭 SQLite；不调用任何外部服务。"""
    store = SQLiteMemoryStore(":memory:")
    try:
        embedder = HashingEmbedder()
        index = DocumentKnowledgeBase(
            document_store=store,
            vector_store=InMemoryVectorStore(embedder.dimension),
            embedder=embedder,
            chunker=DocumentChunker(chunk_size=700, chunk_overlap=80),
            user_id="supportops-project-demo",
        )
        for document in corpus.documents:
            index.ingest(document)
        yield index
    finally:
        store.close()


__all__ = ["SeedCorpus", "load_seed_corpus", "open_seed_index"]
