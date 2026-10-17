"""无需外部模型的可替换文档重排序接口。"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from ..core.exceptions import RAGError
from .models import RetrievedDocumentChunk


_LATIN_PATTERN = re.compile(r"[A-Za-z0-9_]+")
_CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")


class BaseReranker(ABC):
    @abstractmethod
    def rerank(
        self,
        query: str,
        candidates: list[RetrievedDocumentChunk],
        *,
        limit: int,
    ) -> list[RetrievedDocumentChunk]:
        raise NotImplementedError


class HybridReranker(BaseReranker):
    """融合向量相似度与中英文词项覆盖率。"""

    def __init__(self, *, vector_weight: float = 0.75) -> None:
        if (
            isinstance(vector_weight, bool)
            or not isinstance(vector_weight, (int, float))
            or not 0.0 <= float(vector_weight) <= 1.0
        ):
            raise RAGError("vector_weight 必须在 0 到 1 之间。")
        self.vector_weight = float(vector_weight)

    def rerank(
        self,
        query: str,
        candidates: list[RetrievedDocumentChunk],
        *,
        limit: int,
    ) -> list[RetrievedDocumentChunk]:
        if not isinstance(query, str) or not query.strip():
            raise RAGError("重排序查询不能为空。")
        if not isinstance(candidates, list) or not all(
            isinstance(candidate, RetrievedDocumentChunk)
            for candidate in candidates
        ):
            raise RAGError("重排序候选必须是 RetrievedDocumentChunk 列表。")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise RAGError("重排序数量必须是正整数。")
        query_terms = self._terms(query)
        reranked = []
        for candidate in candidates:
            searchable = " ".join(
                filter(
                    None,
                    [
                        candidate.chunk.title,
                        candidate.chunk.section,
                        candidate.chunk.content,
                    ],
                )
            )
            terms = self._terms(searchable)
            lexical = (
                len(query_terms & terms) / len(query_terms)
                if query_terms
                else 0.0
            )
            vector = max(0.0, candidate.vector_score)
            score = vector * self.vector_weight + lexical * (
                1.0 - self.vector_weight
            )
            reranked.append(
                candidate.model_copy(
                    update={"lexical_score": lexical, "score": score}
                )
            )
        reranked.sort(
            key=lambda hit: (hit.score, hit.vector_score, hit.chunk.id),
            reverse=True,
        )
        return reranked[:limit]

    @staticmethod
    def _terms(text: str) -> set[str]:
        lowered = text.casefold()
        latin = {match.group(0) for match in _LATIN_PATTERN.finditer(lowered)}
        cjk = [match.group(0) for match in _CJK_PATTERN.finditer(lowered)]
        terms = latin | set(cjk)
        terms.update("".join(cjk[index : index + 2]) for index in range(len(cjk) - 1))
        return terms


__all__ = ["BaseReranker", "HybridReranker"]
