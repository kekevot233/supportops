"""python -m supportops：离线检索、验收基线和本机应用演示。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hello_agents import HelloAgentsError
from .demo import configure_demo_parser, run_demo
from .errors import SupportOpsError
from .evaluation import evaluate_retrieval, load_acceptance_dataset
from .seed import load_seed_corpus, open_seed_index


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse 默认回显非法参数的原值；误把凭据传入命令行时不能再次输出它。
        self.print_usage(sys.stderr)
        self.exit(2, "SupportOps 参数无效；请使用 --help 查看格式。\n")


def main(argv: list[str] | None = None) -> int:
    parser = _SafeArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    search = subparsers.add_parser("search", help="返回真实召回片段和原文引用，不生成答案")
    search.add_argument("query")
    search.add_argument("--top-k", type=int, default=5)
    evaluate = subparsers.add_parser("evaluate", help="运行固定开发集，应用验收仍标记待实施")
    evaluate.add_argument("--output", type=Path)
    answers = subparsers.add_parser("evaluate-answers", help="离线回答 smoke；如实记录 Fake 的事实、拒答和注入失败")
    answers.add_argument("--output", type=Path)
    preflight = subparsers.add_parser("preflight-pilot", help="无密钥、无网络的 1～3 题配置和预算预检；不执行试跑")
    preflight.add_argument("--config", type=Path, required=True)
    preflight.add_argument("--output", type=Path)
    live = subparsers.add_parser("run-deepseek-pilot", help="显式授权的单次三题试跑，读取专用凭据；不接网页")
    live.add_argument("--authorize-live", action="store_true")
    live.add_argument("--output", type=Path, required=True)
    configure_demo_parser(subparsers)
    from .local_server import configure_serve_parser
    configure_serve_parser(subparsers)
    args = parser.parse_args(argv)
    try:
        if args.command == "run-deepseek-pilot":
            from .live_pilot import run_live_pilot
            payload = run_live_pilot(Path(__file__).resolve().parent.parent, args.output, authorized=args.authorize_live)
            print(json.dumps({"authorization_id": payload["authorization_id"], "summary": payload["summary"],
                "financial_guard": payload["financial_guard"], "report_written": True}, ensure_ascii=False, indent=2))
            return 0 if payload["summary"]["quality_gate_passed"] else 1
        if args.command == "preflight-pilot":
            from .qa_cases import load_answer_dataset
            from .qa_evaluation import resolve_answer_report
            from .qa_preflight import load_pilot_config, preflight_pilot
            root = Path(__file__).resolve().parent.parent
            try:
                target = resolve_answer_report(args.output, project_root=root) if args.output else None
                config = load_pilot_config(args.config, project_root=root)
                corpus = load_seed_corpus()
                payload = preflight_pilot(config, corpus, load_answer_dataset(corpus))
                rendered = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
                if target:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(rendered + "\n", encoding="utf-8")
            except (HelloAgentsError, ValueError, OSError, RecursionError):
                raise SupportOpsError("PREFLIGHT_INVALID") from None
            print(rendered)
            return 0 if payload["offline_checks_passed"] else 1
        if args.command == "evaluate-answers":
            from .qa_cases import load_answer_dataset
            from .qa_evaluation import evaluate_answers, resolve_answer_report
            root = Path(__file__).resolve().parent.parent
            target = resolve_answer_report(args.output, project_root=root) if args.output else None
            corpus = load_seed_corpus()
            payload = evaluate_answers(corpus, load_answer_dataset(corpus))
            rendered = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
            if target:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(rendered + "\n", encoding="utf-8")
            print(rendered)
            return 0 if payload["summary"]["quality_gate_passed"] else 1
        if args.command == "serve":
            from .local_server import run_server
            return run_server(args, project_root=Path(__file__).resolve().parent.parent)
        if args.command == "demo":
            payload = run_demo(args, project_root=Path(__file__).resolve().parent.parent)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        corpus = load_seed_corpus()
        with open_seed_index(corpus) as index:
            if args.command == "search":
                result = index.query(args.query, limit=args.top_k)
                payload = {"mode": "offline_retrieval_only", "model_calls": 0,
                           "corpus_sha256": corpus.sha256, "result": result.model_dump(mode="json")}
                code = 0
            else:
                payload = evaluate_retrieval(index, corpus, load_acceptance_dataset(corpus))
                code = 0 if payload["retrieval_gate_passed"] else 1
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.command == "evaluate" and args.output:
            # 报告只输出到工作区 outputs，避免误覆盖资料或验收集。
            output_root = Path(__file__).resolve().parent.parent / "outputs"
            target = args.output.resolve()
            if not target.is_relative_to(output_root.resolve()) or target.suffix != ".json":
                raise ValueError("评测报告应保存到项目 outputs 目录内的 JSON 文件。")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return code
    except SupportOpsError as error:
        print(json.dumps(error.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    except (HelloAgentsError, ValueError, OSError):
        print("SupportOps 资料加载或检索失败，请检查清单、问题和输出路径。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
