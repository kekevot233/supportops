import unittest

from pydantic import ValidationError

from hello_agents import (
    BaseTokenCounter,
    ContextBuilder,
    ContextConfig,
    ContextEngineeringError,
    ContextPacket,
    HeuristicTokenCounter,
    TokenizerTokenCounter,
)


class CharacterTokenizer:
    """每个字符一个 ID，让测试能独立计算精确预期值。"""

    def __init__(self):
        self.kwargs = []

    def encode(self, text, **kwargs):
        self.kwargs.append(kwargs)
        return [ord(char) for char in text]


class BrokenCounter(BaseTokenCounter):
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    def count_tokens(self, text):
        if self.error:
            raise self.error
        return self.value


def packet(source, content, relevance=1.0):
    return ContextPacket(
        content=content,
        source=source,
        kind="evidence",
        relevance_score=relevance,
    )


def build(builder, packets):
    return builder.build_result(
        "Qdrant",
        custom_packets=packets,
        include_task=False,
        include_output=False,
    )


class TestTokenCounters(unittest.TestCase):
    def test_heuristic_keeps_legacy_static_interface(self):
        counter = HeuristicTokenCounter()
        self.assertEqual(counter.count_tokens("中文abcd"), 3)
        self.assertEqual(counter.count_tokens(""), 0)
        self.assertEqual(
            ContextBuilder.estimate_tokens("中英文 Python"),
            counter.count_tokens("中英文 Python"),
        )

    def test_injected_tokenizer_counts_ids_and_forwards_encode_options(self):
        tokenizer = CharacterTokenizer()
        counter = TokenizerTokenCounter(
            tokenizer,
            name="fake-character-model",
            encode_kwargs={"add_special_tokens": False},
        )
        self.assertEqual(counter.count_tokens("中文 ab"), 5)
        self.assertEqual(counter.count_tokens(""), 0)
        self.assertEqual(tokenizer.kwargs, [{"add_special_tokens": False}])

    def test_invalid_token_ids_and_encoding_errors_are_sanitized(self):
        class FakeTokenizer:
            def __init__(self, value):
                self.value = value

            def encode(self, text):
                if isinstance(self.value, Exception):
                    raise self.value
                return self.value

        for value in ["abc", b"abc", [[1]], [True], [-1], [1.5], [], None,
                      RuntimeError("fake-secret-detail")]:
            with self.subTest(value=type(value).__name__):
                counter = TokenizerTokenCounter(FakeTokenizer(value), name="fake")
                with self.assertRaises(ContextEngineeringError) as caught:
                    counter.count_tokens("资料")
                self.assertNotIn("fake-secret-detail", str(caught.exception))

    def test_bad_counter_dependency_and_outputs_are_rejected(self):
        with self.assertRaises(ContextEngineeringError):
            ContextBuilder(token_counter=lambda text: len(text))
        for value in [True, -1, 0, 1.5, "5"]:
            with self.subTest(value=value):
                builder = ContextBuilder(token_counter=BrokenCounter(value=value))
                with self.assertRaises(ContextEngineeringError):
                    builder.build("问题")

    def test_counter_error_does_not_silently_fall_back_to_heuristic(self):
        builder = ContextBuilder(
            token_counter=BrokenCounter(error=RuntimeError("fake-secret-detail"))
        )
        with self.assertRaises(ContextEngineeringError) as caught:
            build(builder, [packet("docs", "Qdrant")])
        self.assertNotIn("fake-secret-detail", str(caught.exception))
        self.assertIsNone(builder.last_result)

    def test_full_render_and_compression_use_configured_tokenizer(self):
        builder = ContextBuilder(
            config=ContextConfig(max_tokens=320, reserve_ratio=0),
            token_counter=TokenizerTokenCounter(CharacterTokenizer(), name="fake-char"),
        )
        result = build(builder, [packet("docs", "Qdrant 使用端口 6333。" * 100)])
        self.assertTrue(result.selected)
        self.assertTrue(result.compressed)
        self.assertEqual(result.estimated_tokens, len(result.context))
        self.assertLessEqual(len(result.context), 320)
        self.assertEqual(result.token_counter, "fake-char")
        for item in result.selected:
            self.assertEqual(item.estimated_tokens, len(item.packet.content))

    def test_mandatory_text_is_checked_with_configured_counter(self):
        builder = ContextBuilder(
            config=ContextConfig(max_tokens=128, reserve_ratio=0),
            token_counter=TokenizerTokenCounter(CharacterTokenizer(), name="fake-char"),
        )
        with self.assertRaises(ContextEngineeringError):
            builder.build("a" * 120)


class TestSourceBudgets(unittest.TestCase):
    def test_source_cap_keeps_room_for_other_sources_after_compression(self):
        builder = ContextBuilder(config=ContextConfig(
            max_tokens=800, reserve_ratio=0, source_max_tokens={"docs": 50},
        ))
        result = build(builder, [
            packet("docs", "Qdrant 文档细节。" * 100),
            packet("notes", "Qdrant 已完成真实测试。", relevance=0.9),
        ])
        self.assertEqual({item.packet.source for item in result.selected}, {"docs", "notes"})
        self.assertLessEqual(result.source_usage["docs"].content_tokens, 50)
        self.assertIsNone(result.source_usage["notes"].max_tokens)
        self.assertEqual(result.source_usage["docs"].max_tokens, 50)

    def test_source_cap_is_aggregate_not_per_packet(self):
        builder = ContextBuilder(config=ContextConfig(
            max_tokens=1000, reserve_ratio=0, source_max_tokens={"docs": 20},
            enable_compression=False,
        ))
        packets = [packet("docs", "Qdrant 文档内容测试。") for _ in range(4)]
        result = build(builder, packets)
        usage = result.source_usage["docs"]
        self.assertGreater(usage.selected_count, 0)
        self.assertGreater(usage.dropped_count, 0)
        self.assertLessEqual(usage.content_tokens, 20)
        self.assertEqual(usage.selected_count + usage.dropped_count, 4)

    def test_small_cap_uses_injected_counter_even_when_chinese_marker_cannot_fit(self):
        class ByteTokenizer:
            def encode(self, text):
                return list(text.encode("utf-8"))

        builder = ContextBuilder(
            config=ContextConfig(
                max_tokens=1200, reserve_ratio=0, min_compressed_packet_tokens=8,
                source_max_tokens={"docs": 8},
            ),
            token_counter=TokenizerTokenCounter(ByteTokenizer(), name="fake-bytes"),
        )
        result = build(builder, [
            packet("docs", "Qdrant 中文资料。" * 30),
            packet("notes", "Qdrant tests passed.", relevance=0.9),
        ])
        self.assertEqual({item.packet.source for item in result.selected}, {"docs", "notes"})
        self.assertLessEqual(result.source_usage["docs"].content_tokens, 8)
        self.assertEqual(result.estimated_tokens, len(result.context.encode("utf-8")))
        self.assertEqual(result.selected[0].packet.metadata["compression_method"], "token_truncation")

    def test_zero_and_tiny_caps_skip_source_without_stopping_others(self):
        for cap in (0, 3):
            with self.subTest(cap=cap):
                builder = ContextBuilder(config=ContextConfig(
                    max_tokens=800, source_max_tokens={"docs": cap},
                ))
                result = build(builder, [
                    packet("docs", "Qdrant 长篇说明。" * 50),
                    packet("notes", "Qdrant 完成。", relevance=0.9),
                ])
                self.assertEqual([item.packet.source for item in result.selected], ["notes"])
                self.assertEqual(result.source_usage["docs"].content_tokens, 0)
                self.assertEqual(result.source_usage["docs"].dropped_count, 1)

    def test_caps_do_not_truncate_system_or_current_task(self):
        builder = ContextBuilder(config=ContextConfig(
            source_max_tokens={"system": 0, "user_query": 0},
        ))
        result = builder.build_result("完整任务", system_instructions="完整系统规则")
        self.assertIn("完整任务", result.context)
        self.assertIn("完整系统规则", result.context)
        self.assertEqual(result.source_usage, {})

    def test_source_limit_values_and_names_are_strictly_validated(self):
        for limits in ({"docs": -1}, {"docs": True}, {"docs": 1.5},
                       {"docs": "10"}, {" docs": 10}, {"bad\nname": 10}, {"": 10}):
            with self.subTest(limits=limits):
                with self.assertRaises(ValidationError):
                    ContextConfig(source_max_tokens=limits)

    def test_usage_reflects_final_render_and_last_result_is_isolated(self):
        builder = ContextBuilder(config=ContextConfig(max_tokens=180, reserve_ratio=0))
        result = build(builder, [packet("docs", "Qdrant 内容。" * 100)])
        self.assertEqual(result.source_usage["docs"].content_tokens,
                         sum(item.estimated_tokens for item in result.selected))
        saved = builder.last_result
        saved.source_usage["docs"].content_tokens = 99999
        self.assertNotEqual(builder.last_result.source_usage["docs"].content_tokens, 99999)
        with self.assertRaises(ContextEngineeringError):
            builder.build("必需内容" * 1000)
        self.assertIsNone(builder.last_result)


if __name__ == "__main__":
    unittest.main()
