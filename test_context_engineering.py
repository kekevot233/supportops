import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from hello_agents import (
    BaseContextProvider,
    ContextBuilder,
    ContextConfig,
    ContextEngineeringError,
    ContextPacket,
    MemorySystemError,
    Message,
    SimpleAgent,
)


class StaticProvider(BaseContextProvider):
    def __init__(self, content):
        self.content = content
        self.queries = []

    def build_context(self, query):
        self.queries.append(query)
        return self.content


class FailingProvider(BaseContextProvider):
    def build_context(self, query):
        raise MemorySystemError("simulated failure")


class RecordingLLM:
    provider = "fake"

    def __init__(self, response="回答"):
        self.response = response
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append([message.model_copy(deep=True) for message in messages])
        return self.response


class TestContextBuilder(unittest.TestCase):
    def test_config_validates_weights_and_budget(self):
        with self.assertRaises(ValidationError):
            ContextConfig(relevance_weight=0.7)
        with self.assertRaises(ValidationError):
            ContextConfig(max_tokens=128, min_compressed_packet_tokens=120)

        config = ContextConfig(max_tokens=1000, reserve_ratio=0.25)
        self.assertEqual(config.available_tokens, 750)

    def test_build_structures_all_supported_inputs(self):
        provider = StaticProvider("文档证据：Python 适合数据分析")
        builder = ContextBuilder()
        builder.add_provider("docs", provider, kind="evidence", priority=0.8)
        history = [
            Message(role="user", content="我正在开发数据工具"),
            Message(role="assistant", content="可以使用 Python"),
        ]
        custom = ContextPacket(
            content="项目阻塞：内存占用过高",
            source="project_note",
            kind="note",
            relevance_score=0.9,
        )

        context = builder.build(
            "如何优化 Python 内存？",
            conversation_history=history,
            system_instructions="你是数据工程顾问。",
            custom_packets=[custom],
        )

        self.assertEqual(provider.queries, ["如何优化 Python 内存？"])
        for label in (
            "[ROLE_AND_POLICIES]",
            "[TASK]",
            "[EVIDENCE]",
            "[MEMORY_AND_NOTES]",
            "[CONVERSATION]",
            "[OUTPUT]",
        ):
            self.assertIn(label, context)
        self.assertIn("数据而非指令", context)
        self.assertIn("不得编造", context)

    def test_provider_mode_does_not_duplicate_query_or_history(self):
        provider = StaticProvider("Python 项目资料")
        builder = ContextBuilder({"memory": provider})
        llm = RecordingLLM()
        agent = SimpleAgent(
            name="上下文助手",
            llm=llm,
            system_prompt="系统规则",
            context_provider=builder,
        )

        agent.run("Python 问题")

        contents = [message.content for message in llm.calls[0]]
        self.assertEqual(provider.queries, ["Python 问题"])
        self.assertEqual(contents.count("Python 问题"), 1)
        self.assertEqual(contents[0], "系统规则")
        self.assertIn("[CONTEXT]", contents[1])
        self.assertNotIn("[TASK]", contents[1])
        self.assertEqual([m.role for m in agent.get_history()], ["user", "assistant"])

    def test_budget_compresses_large_high_value_packet(self):
        config = ContextConfig(
            max_tokens=256,
            reserve_ratio=0.1,
            min_compressed_packet_tokens=16,
        )
        provider = StaticProvider("知识" * 1000)
        builder = ContextBuilder(config=config)
        builder.add_provider("docs", provider, kind="evidence")

        result = builder.build_result(
            "知识是什么？",
            include_task=False,
            include_output=False,
        )

        self.assertLessEqual(result.estimated_tokens, result.token_budget)
        self.assertTrue(result.compressed)
        self.assertIn("抽取式压缩", result.context)
        self.assertEqual(
            result.selected[0].packet.metadata["compression_method"],
            "ExtractiveContextCompressor",
        )
        self.assertEqual(result.selected[0].packet.source, "docs")

    def test_relevance_and_recency_affect_selection_deterministically(self):
        config = ContextConfig(
            max_tokens=220,
            reserve_ratio=0.0,
            min_relevance=0.1,
            min_compressed_packet_tokens=16,
        )
        builder = ContextBuilder(config=config)
        now = datetime.now(timezone.utc)
        packets = [
            ContextPacket(
                content="Python " + "新" * 180,
                source="recent",
                timestamp=now,
                relevance_score=0.8,
            ),
            ContextPacket(
                content="Python " + "旧" * 180,
                source="old",
                timestamp=now - timedelta(days=365),
                relevance_score=0.8,
            ),
            ContextPacket(
                content="完全无关",
                source="irrelevant",
                timestamp=now,
                relevance_score=0.0,
            ),
        ]

        result = builder.build_result(
            "Python",
            custom_packets=packets,
            include_task=False,
            include_output=False,
        )

        sources = [item.packet.source for item in result.selected]
        self.assertIn("recent", sources)
        self.assertNotIn("irrelevant", sources)
        self.assertGreaterEqual(result.dropped_count, 1)

    def test_fail_open_and_fail_closed_sources(self):
        closed = ContextBuilder()
        closed.add_provider("memory", FailingProvider())
        with self.assertRaises(MemorySystemError):
            closed.build_context("问题")

        opened = ContextBuilder()
        opened.add_provider("memory", FailingProvider(), fail_open=True)
        result = opened.build_result(
            "问题",
            include_task=False,
            include_output=False,
        )
        self.assertEqual(result.context, "")
        self.assertEqual(len(result.warnings), 1)

    def test_mandatory_content_is_never_silently_truncated(self):
        builder = ContextBuilder(
            config=ContextConfig(
                max_tokens=128,
                reserve_ratio=0.0,
                min_compressed_packet_tokens=8,
            )
        )
        with self.assertRaises(ContextEngineeringError):
            builder.build("任务" * 500, system_instructions="规则" * 500)

    def test_source_registration_and_input_validation(self):
        builder = ContextBuilder()
        provider = StaticProvider("资料")
        builder.add_provider("docs", provider)
        with self.assertRaises(ContextEngineeringError):
            builder.add_provider("docs", provider)
        with self.assertRaises(ContextEngineeringError):
            builder.add_provider("bad\nname", provider)
        with self.assertRaises(ContextEngineeringError):
            builder.build_context(" ")


if __name__ == "__main__":
    unittest.main()
