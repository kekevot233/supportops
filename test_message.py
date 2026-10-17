import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from hello_agents import Message


class TestMessage(unittest.TestCase):
    def test_creates_safe_independent_defaults(self):
        first = Message(content="第一条消息", role="user")
        second = Message(content="第二条消息", role="assistant")

        first.metadata["source"] = "test"

        self.assertEqual(second.metadata, {})
        self.assertIsNot(first.metadata, second.metadata)
        self.assertIsNotNone(first.timestamp.tzinfo)

    def test_to_dict_only_returns_api_fields(self):
        message = Message(
            content="调用计算器",
            role="tool",
            metadata={"tool_name": "calculator"},
        )

        self.assertEqual(
            message.to_dict(),
            {"role": "tool", "content": "调用计算器"},
        )

    def test_preserves_explicit_timestamp_and_metadata(self):
        timestamp = datetime(2026, 9, 13, 2, 30, tzinfo=timezone.utc)
        metadata = {"request_id": "fake-request-id"}

        message = Message(
            content="测试",
            role="system",
            timestamp=timestamp,
            metadata=metadata,
        )

        self.assertEqual(message.timestamp, timestamp)
        self.assertEqual(message.metadata, metadata)
        self.assertEqual(str(message), "[system] 测试")

    def test_rejects_unknown_role(self):
        with self.assertRaises(ValidationError):
            Message(content="非法角色", role="unknown")


if __name__ == "__main__":
    unittest.main()
