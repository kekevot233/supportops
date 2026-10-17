import json
import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from hello_agents import (
    MemoryConfig,
    MemoryItem,
    MemoryManager,
    MemorySystemError,
    MemoryTool,
    ToolError,
    ToolRegistry,
    WorkingMemory,
)


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, *, seconds):
        self.now += timedelta(seconds=seconds)


def make_item(clock, memory_id, content, importance=0.5):
    return MemoryItem(
        id=memory_id,
        content=content,
        importance=importance,
        created_at=clock.now,
        accessed_at=clock.now,
    )


class TestMemoryModels(unittest.TestCase):
    def test_memory_item_normalizes_text_and_owns_mutable_defaults(self):
        first = MemoryItem(content="  用户喜欢 Python  ")
        second = MemoryItem(content="另一条")

        self.assertEqual(first.content, "用户喜欢 Python")
        self.assertNotEqual(first.id, second.id)
        first.metadata["topic"] = "python"
        self.assertEqual(second.metadata, {})

    def test_memory_item_rejects_invalid_fields_and_naive_time(self):
        with self.assertRaises(ValidationError):
            MemoryItem(content="   ")
        with self.assertRaises(ValidationError):
            MemoryItem(content="内容", importance=1.1)
        with self.assertRaises(ValidationError):
            MemoryItem(content="内容", created_at=datetime(2026, 1, 1))

    def test_memory_config_validates_capacity_ttl_and_limits(self):
        with self.assertRaises(ValidationError):
            MemoryConfig(working_memory_capacity=0)
        with self.assertRaises(ValidationError):
            MemoryConfig(working_memory_ttl_seconds=0)
        with self.assertRaises(ValidationError):
            MemoryConfig(perceptual_recency_decay_hours=0)
        with self.assertRaises(ValidationError):
            MemoryConfig(perceptual_candidate_multiplier=21)
        with self.assertRaises(ValidationError):
            MemoryConfig(default_retrieval_limit=6, max_retrieval_limit=5)


class TestWorkingMemory(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.config = MemoryConfig(
            working_memory_capacity=2,
            working_memory_ttl_seconds=60,
            default_retrieval_limit=2,
            max_retrieval_limit=10,
        )
        self.memory = WorkingMemory(self.config, clock=self.clock)

    def test_add_and_list_use_deep_copies(self):
        item = make_item(self.clock, "one", "用户喜欢 Python")
        item.metadata["nested"] = {"value": 1}

        self.memory.add(item)
        item.content = "外部修改"
        item.metadata["nested"]["value"] = 2
        listed = self.memory.list_items()
        listed[0].metadata["nested"]["value"] = 3

        fresh = self.memory.list_items()[0]
        self.assertEqual(fresh.content, "用户喜欢 Python")
        self.assertEqual(fresh.metadata["nested"]["value"], 1)
        self.assertEqual(
            fresh.expires_at,
            self.clock.now + timedelta(seconds=60),
        )

    def test_expired_items_are_removed_lazily(self):
        self.memory.add(make_item(self.clock, "one", "短期任务"))
        self.clock.advance(seconds=60)

        self.assertEqual(len(self.memory), 0)
        self.assertEqual(self.memory.list_items(), [])

    def test_capacity_evicts_lowest_value_item(self):
        self.memory.add(make_item(self.clock, "low", "低重要性", 0.1))
        self.clock.advance(seconds=1)
        self.memory.add(make_item(self.clock, "high", "高重要性", 0.9))
        self.clock.advance(seconds=1)
        self.memory.add(make_item(self.clock, "new", "新记忆", 0.5))

        self.assertEqual(
            {item.id for item in self.memory.list_items()},
            {"high", "new"},
        )

    def test_retrieve_ranks_relevance_and_updates_access_data(self):
        self.memory.add(
            make_item(self.clock, "python", "用户喜欢 Python 编程", 0.8)
        )
        self.memory.add(
            make_item(self.clock, "travel", "用户计划去上海旅行", 0.8)
        )

        results = self.memory.retrieve("Python 喜好", limit=2)

        self.assertEqual(results[0].item.id, "python")
        self.assertGreater(results[0].score, 0)
        self.assertEqual(results[0].item.access_count, 1)
        stored = {
            item.id: item for item in self.memory.list_items()
        }
        self.assertEqual(stored["python"].access_count, 1)
        self.assertEqual(stored["travel"].access_count, 0)

    def test_retrieve_supports_chinese_and_importance_filter(self):
        self.memory.add(make_item(self.clock, "low", "上海天气晴朗", 0.2))
        self.memory.add(make_item(self.clock, "high", "上海地铁线路", 0.9))

        results = self.memory.retrieve(
            "上海",
            limit=5,
            min_importance=0.5,
        )

        self.assertEqual([result.item.id for result in results], ["high"])

    def test_retrieve_rejects_bad_query_limit_and_importance(self):
        with self.assertRaises(MemorySystemError):
            self.memory.retrieve(" ", limit=1)
        with self.assertRaises(MemorySystemError):
            self.memory.retrieve("query", limit=0)
        with self.assertRaises(MemorySystemError):
            self.memory.retrieve("query", limit=11)
        with self.assertRaises(MemorySystemError):
            self.memory.retrieve("query", limit=1, min_importance=2)

    def test_remove_clear_and_duplicate_id(self):
        self.memory.add(make_item(self.clock, "one", "第一条"))
        with self.assertRaises(MemorySystemError):
            self.memory.add(make_item(self.clock, "one", "重复"))

        removed = self.memory.remove("one")
        self.assertEqual(removed.content, "第一条")
        with self.assertRaises(MemorySystemError):
            self.memory.remove("missing")

        self.memory.add(make_item(self.clock, "two", "第二条"))
        self.assertEqual(self.memory.clear(), 1)
        self.assertEqual(self.memory.clear(), 0)

    def test_rejects_wrong_memory_type_and_naive_clock(self):
        with self.assertRaises(MemorySystemError):
            self.memory.add(
                MemoryItem(content="事件", memory_type="episodic")
            )
        invalid = WorkingMemory(clock=lambda: datetime(2026, 1, 1))
        with self.assertRaises(MemorySystemError):
            len(invalid)


class TestMemoryManager(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.config = MemoryConfig(default_retrieval_limit=3)
        self.backend = WorkingMemory(self.config, clock=self.clock)
        self.manager = MemoryManager(
            user_id="alice",
            config=self.config,
            backends={"working": self.backend},
        )

    def test_add_retrieve_stats_and_remove(self):
        memory_id = self.manager.add_memory(
            "Alice 喜欢 Python",
            importance=0.8,
            metadata={"source": "chat", "user_id": "wrong"},
        )

        stored = self.backend.list_items()[0]
        self.assertEqual(stored.metadata["user_id"], "alice")
        self.assertEqual(stored.metadata["source"], "chat")
        results = self.manager.retrieve_memories("Python")
        self.assertEqual(results[0].item.id, memory_id)
        self.assertEqual(
            self.manager.get_stats(),
            {"user_id": "alice", "total": 1, "by_type": {"working": 1}},
        )
        self.assertEqual(self.manager.remove_memory(memory_id).id, memory_id)

    def test_rejects_unknown_or_unconfigured_memory_types(self):
        with self.assertRaises(MemorySystemError):
            self.manager.add_memory("内容", memory_type="episodic")
        with self.assertRaises(MemorySystemError):
            self.manager.retrieve_memories(
                "内容",
                memory_types=["semantic"],
            )
        with self.assertRaises(MemorySystemError):
            MemoryManager(user_id=" ")


class TestMemoryTool(unittest.TestCase):
    def setUp(self):
        self.manager = MemoryManager(user_id="tool-user")
        self.tool = MemoryTool(self.manager)

    def test_schema_and_full_action_flow(self):
        schema = self.tool.to_openai_schema()["function"]
        self.assertEqual(schema["name"], "memory")
        self.assertEqual(schema["parameters"]["required"], ["action"])

        added = self.tool.run(
            {
                "action": "add",
                "content": "用户偏好使用 Python",
                "importance": 0.9,
            }
        )
        memory_id = added.split("ID：", 1)[1].removesuffix("。")
        searched = self.tool.run(
            {"action": "search", "query": "Python"}
        )
        stats = json.loads(self.tool.run({"action": "stats"}))
        removed = self.tool.run(
            {"action": "remove", "memory_id": memory_id}
        )

        self.assertIn("用户偏好使用 Python", searched)
        self.assertEqual(stats["total"], 1)
        self.assertIn(memory_id, removed)

    def test_invalid_actions_and_conditional_parameters_are_rejected(self):
        with self.assertRaises(ToolError):
            self.tool.run({"action": "unknown"})
        with self.assertRaises(ToolError):
            self.tool.run({"action": "add"})
        with self.assertRaises(ToolError):
            self.tool.run({"action": "search"})
        with self.assertRaises(ToolError):
            self.tool.run({"action": "remove"})

    def test_registry_can_execute_memory_tool(self):
        registry = ToolRegistry()
        registry.register_tool(self.tool)

        result = registry.execute_tool(
            "memory",
            {"action": "add", "content": "通过注册表保存"},
        )

        self.assertIn("已添加 working 记忆", result)
        self.assertEqual(self.manager.get_stats()["total"], 1)


if __name__ == "__main__":
    unittest.main()
