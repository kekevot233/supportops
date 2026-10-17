"""知识抽取结果在写入图存储前的确定性质量检查。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.exceptions import MemorySystemError
from .extractors import ExtractedKnowledge


QualitySeverity = Literal["warning", "error"]


class KnowledgeQualityIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: QualitySeverity
    entity_id: str | None = None
    relation_id: str | None = None


class KnowledgeQualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool
    score: float = Field(ge=0.0, le=1.0)
    entity_count: int = Field(ge=0)
    relation_count: int = Field(ge=0)
    issues: list[KnowledgeQualityIssue] = Field(default_factory=list)


class KnowledgeGraphValidator:
    """检查图谱身份、来源、关系端点和重复项。"""

    def validate(
        self,
        knowledge: ExtractedKnowledge,
        *,
        expected_user_id: str | None = None,
        expected_memory_id: str | None = None,
    ) -> KnowledgeQualityReport:
        if not isinstance(knowledge, ExtractedKnowledge):
            raise MemorySystemError("knowledge 必须是 ExtractedKnowledge。")
        expected_user_id = self._normalize_optional_id(
            expected_user_id, "expected_user_id"
        )
        expected_memory_id = self._normalize_optional_id(
            expected_memory_id, "expected_memory_id"
        )
        issues: list[KnowledgeQualityIssue] = []
        entity_ids: set[str] = set()
        identities: set[tuple[str, str]] = set()

        for entity in knowledge.entities:
            if entity.id in entity_ids:
                issues.append(
                    self._issue(
                        "duplicate_entity_id",
                        "存在重复的实体 ID。",
                        "error",
                        entity_id=entity.id,
                    )
                )
            entity_ids.add(entity.id)
            identity = (entity.entity_type.casefold(), entity.name.casefold())
            if identity in identities:
                issues.append(
                    self._issue(
                        "duplicate_entity_identity",
                        "存在类型和名称均相同的重复实体。",
                        "error",
                        entity_id=entity.id,
                    )
                )
            identities.add(identity)
            if expected_user_id is not None and entity.user_id != expected_user_id:
                issues.append(
                    self._issue(
                        "entity_user_mismatch",
                        "实体不属于预期用户。",
                        "error",
                        entity_id=entity.id,
                    )
                )
            if (
                expected_memory_id is not None
                and expected_memory_id not in entity.source_memory_ids
            ):
                issues.append(
                    self._issue(
                        "entity_source_mismatch",
                        "实体缺少预期的来源记忆 ID。",
                        "error",
                        entity_id=entity.id,
                    )
                )

        relation_ids: set[str] = set()
        signatures: set[tuple[str, str, str]] = set()
        for relation in knowledge.relations:
            if relation.id in relation_ids:
                issues.append(
                    self._issue(
                        "duplicate_relation_id",
                        "存在重复的关系 ID。",
                        "error",
                        relation_id=relation.id,
                    )
                )
            relation_ids.add(relation.id)
            signature = (
                relation.source_id,
                relation.relation_type.casefold(),
                relation.target_id,
            )
            if signature in signatures:
                issues.append(
                    self._issue(
                        "duplicate_relation",
                        "存在起点、类型和终点均相同的重复关系。",
                        "error",
                        relation_id=relation.id,
                    )
                )
            signatures.add(signature)
            if relation.source_id not in entity_ids:
                issues.append(
                    self._issue(
                        "missing_source_entity",
                        "关系的起点实体不存在。",
                        "error",
                        relation_id=relation.id,
                    )
                )
            if relation.target_id not in entity_ids:
                issues.append(
                    self._issue(
                        "missing_target_entity",
                        "关系的终点实体不存在。",
                        "error",
                        relation_id=relation.id,
                    )
                )
            if relation.source_id == relation.target_id:
                issues.append(
                    self._issue(
                        "self_relation",
                        "关系的起点和终点是同一个实体。",
                        "warning",
                        relation_id=relation.id,
                    )
                )
            if expected_user_id is not None and relation.user_id != expected_user_id:
                issues.append(
                    self._issue(
                        "relation_user_mismatch",
                        "关系不属于预期用户。",
                        "error",
                        relation_id=relation.id,
                    )
                )
            if (
                expected_memory_id is not None
                and relation.source_memory_id != expected_memory_id
            ):
                issues.append(
                    self._issue(
                        "relation_source_mismatch",
                        "关系不属于预期来源记忆。",
                        "error",
                        relation_id=relation.id,
                    )
                )

        error_count = sum(issue.severity == "error" for issue in issues)
        warning_count = sum(issue.severity == "warning" for issue in issues)
        score = max(0.0, 1.0 - error_count * 0.2 - warning_count * 0.05)
        return KnowledgeQualityReport(
            valid=error_count == 0,
            score=score,
            entity_count=len(knowledge.entities),
            relation_count=len(knowledge.relations),
            issues=issues,
        )

    @staticmethod
    def _normalize_optional_id(value: object, name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise MemorySystemError(f"{name} 必须是非空字符串或 None。")
        return value.strip()

    @staticmethod
    def _issue(
        code: str,
        message: str,
        severity: QualitySeverity,
        *,
        entity_id: str | None = None,
        relation_id: str | None = None,
    ) -> KnowledgeQualityIssue:
        return KnowledgeQualityIssue(
            code=code,
            message=message,
            severity=severity,
            entity_id=entity_id,
            relation_id=relation_id,
        )


__all__ = [
    "KnowledgeGraphValidator",
    "KnowledgeQualityIssue",
    "KnowledgeQualityReport",
    "QualitySeverity",
]
