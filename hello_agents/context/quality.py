"""上下文质量的确定性评估。"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .models import ContextQualityReport, SelectedContextPacket


_TERM_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class ContextQualityEvaluator:
    """从相关性、密度、覆盖率、预算和来源多样性评估上下文。"""

    def evaluate(
        self,
        *,
        query: str,
        selected: Sequence[SelectedContextPacket],
        estimated_tokens: int,
        token_budget: int,
        dropped_count: int,
        compressed: bool,
    ) -> ContextQualityReport:
        optional = [
            item for item in selected if item.packet.kind not in {"system", "task"}
        ]
        selected_count = len(selected)
        trusted_count = sum(item.packet.trusted for item in selected)
        untrusted_count = selected_count - trusted_count
        if optional:
            average_score = sum(item.score for item in optional) / len(optional)
            token_total = sum(item.estimated_tokens for item in optional)
            information_density = (
                sum(item.score * item.estimated_tokens for item in optional)
                / max(1, token_total)
            )
            unique_sources = len({item.packet.source for item in optional})
            source_diversity = unique_sources / len(optional)
        else:
            average_score = 0.0
            information_density = 0.0
            source_diversity = 0.0

        query_terms = set(_TERM_PATTERN.findall(query.lower()))
        context_terms: set[str] = set()
        for item in optional:
            context_terms.update(_TERM_PATTERN.findall(item.packet.content.lower()))
        query_coverage = (
            len(query_terms & context_terms) / len(query_terms)
            if query_terms
            else 0.0
        )
        budget_utilization = min(1.0, estimated_tokens / max(1, token_budget))
        overall_score = (
            average_score * 0.35
            + information_density * 0.30
            + query_coverage * 0.25
            + source_diversity * 0.10
        )

        suggestions: list[str] = []
        if not optional:
            suggestions.append("没有选入外部参考信息；请检查检索来源或相关性阈值。")
        else:
            if average_score < 0.5:
                suggestions.append("入选信息平均得分偏低；建议提高检索质量或相关性阈值。")
            if information_density < 0.5:
                suggestions.append("上下文信息密度偏低；建议去重并缩短低价值片段。")
            if query_coverage < 0.5:
                suggestions.append("当前资料对查询词项覆盖不足；建议扩展检索或改写查询。")
            if source_diversity < 0.5 and len(optional) > 1:
                suggestions.append("信息来源较集中；关键结论可增加独立来源交叉验证。")
        if budget_utilization > 0.9:
            suggestions.append("上下文预算使用超过 90%；建议为后续工具结果保留更多空间。")
        if compressed:
            suggestions.append("本轮发生了压缩；关键决策应核对原始来源。")
        if dropped_count > 0 and query_coverage < 1.0:
            suggestions.append("存在被丢弃信息且查询覆盖不完整；可适当提高预算或调整来源优先级。")

        return ContextQualityReport(
            overall_score=round(_clamp(overall_score), 4),
            average_score=round(_clamp(average_score), 4),
            information_density=round(_clamp(information_density), 4),
            query_coverage=round(_clamp(query_coverage), 4),
            budget_utilization=round(_clamp(budget_utilization), 4),
            source_diversity=round(_clamp(source_diversity), 4),
            selected_count=selected_count,
            trusted_count=trusted_count,
            untrusted_count=untrusted_count,
            suggestions=suggestions,
        )


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


__all__ = ["ContextQualityEvaluator"]
