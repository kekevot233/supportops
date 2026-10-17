"""用于离线测试的小型进程内图存储。"""

from __future__ import annotations

import re
from collections import defaultdict
from threading import RLock

from ..core.exceptions import GraphStoreError
from .base import BaseGraphStore, GraphEntity, GraphRelation, GraphSearchResult


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


class InMemoryGraphStore(BaseGraphStore):
    def __init__(self) -> None:
        self._entities: dict[str, GraphEntity] = {}
        self._relations: dict[str, GraphRelation] = {}
        self._lock = RLock()

    def upsert(
        self,
        entities: list[GraphEntity],
        relations: list[GraphRelation],
    ) -> None:
        if not all(isinstance(item, GraphEntity) for item in entities):
            raise GraphStoreError("entities 必须全部是 GraphEntity。")
        if not all(isinstance(item, GraphRelation) for item in relations):
            raise GraphStoreError("relations 必须全部是 GraphRelation。")
        with self._lock:
            staged_entities = {
                key: value.model_copy(deep=True)
                for key, value in self._entities.items()
            }
            for entity in entities:
                existing = staged_entities.get(entity.id)
                if existing and existing.user_id != entity.user_id:
                    raise GraphStoreError("不能用其他用户的实体覆盖现有实体。")
                if existing:
                    sources = list(existing.source_memory_ids)
                    for source in entity.source_memory_ids:
                        if source not in sources:
                            sources.append(source)
                    entity = entity.model_copy(
                        update={"source_memory_ids": sources}, deep=True
                    )
                staged_entities[entity.id] = entity.model_copy(deep=True)

            staged_relations = {
                key: value.model_copy(deep=True)
                for key, value in self._relations.items()
            }
            for relation in relations:
                source = staged_entities.get(relation.source_id)
                target = staged_entities.get(relation.target_id)
                if source is None or target is None:
                    raise GraphStoreError("关系的起点和终点实体必须存在。")
                if (
                    source.user_id != relation.user_id
                    or target.user_id != relation.user_id
                ):
                    raise GraphStoreError("关系与端点实体必须属于同一用户。")
                existing = staged_relations.get(relation.id)
                if existing and existing.user_id != relation.user_id:
                    raise GraphStoreError("不能用其他用户的关系覆盖现有关系。")
                staged_relations[relation.id] = relation.model_copy(deep=True)

            self._entities = staged_entities
            self._relations = staged_relations

    def search(
        self,
        query: str,
        *,
        user_id: str,
        limit: int,
    ) -> list[GraphSearchResult]:
        terms = self._terms(query)
        if not isinstance(user_id, str) or not user_id.strip():
            raise GraphStoreError("图检索用户 ID 不能为空。")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise GraphStoreError("图检索数量必须是正整数。")

        scores: defaultdict[str, float] = defaultdict(float)
        matches: defaultdict[str, set[str]] = defaultdict(set)
        with self._lock:
            entities = [e.model_copy(deep=True) for e in self._entities.values()]
            relations = [r.model_copy(deep=True) for r in self._relations.values()]
            entity_by_id = {entity.id: entity for entity in entities}

        for entity in entities:
            if entity.user_id != user_id:
                continue
            text = f"{entity.name} {entity.entity_type}".lower()
            matched = sum(term in text for term in terms)
            if matched:
                contribution = matched / len(terms)
                for memory_id in entity.source_memory_ids:
                    scores[memory_id] += contribution
                    matches[memory_id].add(entity.id)

        for relation in relations:
            if relation.user_id != user_id:
                continue
            source = entity_by_id.get(relation.source_id)
            target = entity_by_id.get(relation.target_id)
            text = " ".join(
                [
                    source.name if source else "",
                    relation.relation_type,
                    target.name if target else "",
                ]
            ).lower()
            matched = sum(term in text for term in terms)
            if matched:
                scores[relation.source_memory_id] += matched / len(terms)
                matches[relation.source_memory_id].update(
                    [relation.source_id, relation.target_id]
                )

        results = [
            GraphSearchResult(
                memory_id=memory_id,
                score=min(1.0, score),
                matched_entity_ids=sorted(matches[memory_id]),
            )
            for memory_id, score in scores.items()
        ]
        results.sort(key=lambda item: (item.score, item.memory_id), reverse=True)
        return results[:limit]

    def delete_memory(self, memory_id: str, *, user_id: str) -> None:
        with self._lock:
            self._relations = {
                key: relation
                for key, relation in self._relations.items()
                if not (
                    relation.user_id == user_id
                    and relation.source_memory_id == memory_id
                )
            }
            for entity_id, entity in list(self._entities.items()):
                if entity.user_id != user_id:
                    continue
                sources = [
                    source for source in entity.source_memory_ids
                    if source != memory_id
                ]
                if sources:
                    self._entities[entity_id] = entity.model_copy(
                        update={"source_memory_ids": sources}, deep=True
                    )
                else:
                    del self._entities[entity_id]

    def clear_user(self, user_id: str) -> None:
        with self._lock:
            self._relations = {
                key: value for key, value in self._relations.items()
                if value.user_id != user_id
            }
            self._entities = {
                key: value for key, value in self._entities.items()
                if value.user_id != user_id
            }

    def stats(self, user_id: str) -> dict[str, int]:
        with self._lock:
            return {
                "entities": sum(e.user_id == user_id for e in self._entities.values()),
                "relations": sum(r.user_id == user_id for r in self._relations.values()),
            }

    @staticmethod
    def _terms(query: object) -> list[str]:
        if not isinstance(query, str) or not query.strip():
            raise GraphStoreError("图检索词不能为空。")
        terms = [token.lower() for token in _TOKEN_PATTERN.findall(query)]
        if not terms:
            raise GraphStoreError("图检索词不包含可识别词项。")
        return terms


__all__ = ["InMemoryGraphStore"]
