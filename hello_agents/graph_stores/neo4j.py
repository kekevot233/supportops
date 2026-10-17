"""Neo4j 知识图谱适配器。"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

from ..core.exceptions import GraphStoreError
from .base import BaseGraphStore, GraphEntity, GraphRelation, GraphSearchResult


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")


class Neo4jGraphStore(BaseGraphStore):
    def __init__(
        self,
        *,
        uri: str | None = None,
        username: str | None = None,
        password: str | None = None,
        database: str = "neo4j",
        driver: Any | None = None,
    ) -> None:
        if driver is None and not all(
            isinstance(value, str) and value.strip()
            for value in (uri, username, password)
        ):
            raise GraphStoreError("未注入驱动时必须提供 Neo4j 连接配置。")
        if not isinstance(database, str) or not database.strip():
            raise GraphStoreError("Neo4j database 不能为空。")
        self.uri = uri.strip() if isinstance(uri, str) else None
        self.username = username
        self.password = password
        self.database = database.strip()
        self._driver = driver

    def upsert(
        self,
        entities: list[GraphEntity],
        relations: list[GraphRelation],
    ) -> None:
        if not all(isinstance(item, GraphEntity) for item in entities):
            raise GraphStoreError("entities 必须全部是 GraphEntity。")
        if not all(isinstance(item, GraphRelation) for item in relations):
            raise GraphStoreError("relations 必须全部是 GraphRelation。")
        if entities:
            rows = [
                {
                    "id": entity.id,
                    "name": entity.name,
                    "entity_type": entity.entity_type,
                    "user_id": entity.user_id,
                    "source_memory_ids": entity.source_memory_ids,
                    "properties_json": json.dumps(
                        entity.properties, ensure_ascii=False, sort_keys=True
                    ),
                }
                for entity in entities
            ]
            self._execute(
                """
                UNWIND $rows AS row
                MERGE (e:MemoryEntity {id: row.id, user_id: row.user_id})
                SET e.name = row.name,
                    e.entity_type = row.entity_type,
                    e.source_memory_ids = reduce(
                        ids = coalesce(e.source_memory_ids, []),
                        memory_id IN row.source_memory_ids |
                        CASE WHEN memory_id IN ids
                             THEN ids ELSE ids + memory_id END
                    ),
                    e.properties_json = row.properties_json
                """,
                {"rows": rows},
            )
        if relations:
            rows = [
                {
                    "id": relation.id,
                    "source_id": relation.source_id,
                    "target_id": relation.target_id,
                    "relation_type": relation.relation_type,
                    "user_id": relation.user_id,
                    "source_memory_id": relation.source_memory_id,
                    "properties_json": json.dumps(
                        relation.properties, ensure_ascii=False, sort_keys=True
                    ),
                }
                for relation in relations
            ]
            self._execute(
                """
                UNWIND $rows AS row
                MATCH (source:MemoryEntity {id: row.source_id,
                                            user_id: row.user_id})
                MATCH (target:MemoryEntity {id: row.target_id,
                                            user_id: row.user_id})
                MERGE (source)-[r:RELATED {id: row.id}]->(target)
                SET r.relation_type = row.relation_type,
                    r.user_id = row.user_id,
                    r.source_memory_id = row.source_memory_id,
                    r.properties_json = row.properties_json
                """,
                {"rows": rows},
            )

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
        parameters = {
            "terms": terms,
            "user_id": user_id.strip(),
            "limit": limit * 2,
        }
        entity_records = self._execute(
            """
            MATCH (e:MemoryEntity {user_id: $user_id})
            WHERE any(term IN $terms WHERE
                toLower(e.name) CONTAINS term OR
                toLower(e.entity_type) CONTAINS term)
            UNWIND e.source_memory_ids AS memory_id
            RETURN memory_id, count(DISTINCT e) AS matched,
                   collect(DISTINCT e.id) AS entity_ids
            LIMIT $limit
            """,
            parameters,
        )
        relation_records = self._execute(
            """
            MATCH (source:MemoryEntity)-[r:RELATED]->(target:MemoryEntity)
            WHERE r.user_id = $user_id AND any(term IN $terms WHERE
                toLower(source.name) CONTAINS term OR
                toLower(target.name) CONTAINS term OR
                toLower(r.relation_type) CONTAINS term)
            RETURN r.source_memory_id AS memory_id, count(DISTINCT r) AS matched,
                   collect(DISTINCT source.id) + collect(DISTINCT target.id)
                       AS entity_ids
            LIMIT $limit
            """,
            parameters,
        )
        scores: defaultdict[str, float] = defaultdict(float)
        matches: defaultdict[str, set[str]] = defaultdict(set)
        for record in [*entity_records, *relation_records]:
            memory_id = str(record["memory_id"])
            scores[memory_id] += float(record["matched"]) / len(terms)
            matches[memory_id].update(record["entity_ids"] or [])
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
        self._execute(
            """
            MATCH ()-[r:RELATED {user_id: $user_id,
                                 source_memory_id: $memory_id}]->()
            DELETE r
            """,
            {"memory_id": memory_id, "user_id": user_id},
        )
        self._execute(
            """
            MATCH (e:MemoryEntity {user_id: $user_id})
            WHERE $memory_id IN e.source_memory_ids
            SET e.source_memory_ids = [id IN e.source_memory_ids
                                       WHERE id <> $memory_id]
            WITH e WHERE size(e.source_memory_ids) = 0
            DETACH DELETE e
            """,
            {"memory_id": memory_id, "user_id": user_id},
        )

    def clear_user(self, user_id: str) -> None:
        self._execute(
            "MATCH (e:MemoryEntity {user_id: $user_id}) DETACH DELETE e",
            {"user_id": user_id},
        )

    def stats(self, user_id: str) -> dict[str, int]:
        records = self._execute(
            """
            MATCH (e:MemoryEntity {user_id: $user_id})
            OPTIONAL MATCH (e)-[r:RELATED {user_id: $user_id}]->()
            RETURN count(DISTINCT e) AS entities,
                   count(DISTINCT r) AS relations
            """,
            {"user_id": user_id},
        )
        if not records:
            return {"entities": 0, "relations": 0}
        return {
            "entities": int(records[0]["entities"]),
            "relations": int(records[0]["relations"]),
        }

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()

    def _execute(self, query: str, parameters: dict[str, Any]) -> list[Any]:
        try:
            result = self._get_driver().execute_query(
                query,
                parameters_=parameters,
                database_=self.database,
            )
            if hasattr(result, "records"):
                return list(result.records)
            if isinstance(result, tuple):
                return list(result[0])
            return list(result)
        except Exception as error:
            raise GraphStoreError("Neo4j 图数据库操作失败。") from error

    def _get_driver(self) -> Any:
        if self._driver is not None:
            return self._driver
        try:
            from neo4j import GraphDatabase
        except ImportError as error:
            raise GraphStoreError("使用 Neo4j 需要安装 neo4j 驱动。") from error
        try:
            self._driver = GraphDatabase.driver(
                self.uri,
                auth=(self.username, self.password),
            )
        except Exception as error:
            raise GraphStoreError("Neo4j 驱动初始化失败。") from error
        return self._driver

    @staticmethod
    def _terms(query: object) -> list[str]:
        if not isinstance(query, str) or not query.strip():
            raise GraphStoreError("图检索词不能为空。")
        terms = [token.lower() for token in _TOKEN_PATTERN.findall(query)]
        if not terms:
            raise GraphStoreError("图检索词不包含可识别词项。")
        return terms


__all__ = ["Neo4jGraphStore"]
