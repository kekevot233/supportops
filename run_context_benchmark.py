"""运行不调用模型、不联网的上下文选择与事实保留基准。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from hello_agents import ContextBuilder, ContextConfig, ContextPacket


class ContextBenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    query: str = Field(min_length=1)
    packets: list[ContextPacket] = Field(max_length=100)
    max_tokens: int = Field(default=512, ge=128, le=100_000)
    source_max_tokens: dict[
        str, Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
    ] = Field(default_factory=dict)
    expected_sources: list[str] = Field(default_factory=list)
    forbidden_sources: list[str] = Field(default_factory=list)
    expected_fragments: list[str] = Field(default_factory=list)
    minimum_query_coverage: float = Field(default=0, ge=0, le=1)
    expect_compression: bool = False


_CASES = TypeAdapter(Annotated[list[ContextBenchmarkCase], Field(min_length=1, max_length=100)])


def load_cases(path: Path) -> list[ContextBenchmarkCase]:
    cases = _CASES.validate_json(path.read_text(encoding="utf-8"))
    if len({case.name for case in cases}) != len(cases):
        raise ValueError("基准名称不能重复。")
    return cases


def evaluate_cases(cases: list[ContextBenchmarkCase]) -> dict:
    """评估可核对的选择结果与事实片段；质量分只作为启发式诊断返回。"""
    if not cases:
        raise ValueError("至少需要一个基准案例。")
    reports = []
    for case in cases:
        # 基准关闭时间权重，使结果不随运行日期漂移。
        config = ContextConfig(
            max_tokens=case.max_tokens,
            reserve_ratio=0,
            relevance_weight=0.8,
            recency_weight=0,
            priority_weight=0.2,
            source_max_tokens=case.source_max_tokens,
        )
        builder = ContextBuilder(config=config)
        result = builder.build_result(
            case.query,
            custom_packets=case.packets,
            include_task=False,
            include_output=False,
        )
        sources = {item.packet.source for item in result.selected}
        # 只在入选正文中检查事实，避免标题或来源名让断言虚假通过。
        selected_text = "\n".join(item.packet.content for item in result.selected)
        missing_fragments = [text for text in case.expected_fragments if text not in selected_text]
        missing_sources = sorted(set(case.expected_sources) - sources)
        forbidden_present = sorted(set(case.forbidden_sources) & sources)
        checks = {
            "within_budget": result.estimated_tokens <= result.token_budget,
            "within_source_limits": all(
                usage.max_tokens is None or usage.content_tokens <= usage.max_tokens
                for usage in result.source_usage.values()
            ),
            "expected_sources_retained": not missing_sources,
            "forbidden_sources_excluded": not forbidden_present,
            "expected_facts_retained": not missing_fragments,
            "query_coverage_sufficient": result.quality.query_coverage >= case.minimum_query_coverage,
            "compression_matches": result.compressed == case.expect_compression,
        }
        reports.append({
            "name": case.name,
            "passed": all(checks.values()),
            "checks": checks,
            "selected_sources": sorted(sources),
            "missing_sources": missing_sources,
            "forbidden_sources_present": forbidden_present,
            "missing_fragments": missing_fragments,
            "estimated_tokens": result.estimated_tokens,
            "token_budget": result.token_budget,
            "token_counter": result.token_counter,
            "source_usage": {name: usage.model_dump() for name, usage in result.source_usage.items()},
            "quality": result.quality.model_dump(),
        })
    passed = sum(report["passed"] for report in reports)
    return {
        "schema_version": 1,
        "case_count": len(reports),
        "passed_count": passed,
        "pass_rate": passed / len(reports),
        "cases": reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases", type=Path,
        default=Path(__file__).parent / "benchmarks" / "context_cases.json",
    )
    parser.add_argument("--output", type=Path, help="可选 JSON 报告输出路径")
    args = parser.parse_args()
    report = evaluate_cases(load_cases(args.cases))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed_count"] == report["case_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
