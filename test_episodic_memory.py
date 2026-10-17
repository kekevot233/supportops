import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from hello_agents import (
    EpisodicMemory,
    HashingEmbedder,
    InMemoryVectorStore,
    MemoryConfig,
    MemoryItem,
    MemoryManager,
    MemorySystemError,
    MemoryTool,
    SQLiteMemoryStore,
    VectorStoreError,
)


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, *, days=0, seconds=0):
        self.now += timedelta(days=days, seconds=seconds)


class FailingVectorStore(InMemoryVectorStore):
    def upsert(self, records):
        raise VectorStoreError("simulated failure")


def make_episode(
    memory_id,
    content,
    *,
    user_id="alice",
    importance=0.5,
    created_at=None,
):
    timestamp = created_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return MemoryItem(
        id=memory_id,
        content=content,
        memory_type="episodic",
        importance=importance,
        created_at=timestamp,
        accessed_at=timestamp,
        metadata={"user_id": user_id, "session_id": "session-1"},
    )


class TestSQLiteMemoryStore(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteMemoryStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_add_get_list_filter_update_and_delete(self):
        first = make_episode("one", "完成 Python 项目", importance=0.8)
        second = make_episode(
            "two",
            "学习数据库",
            user_id="bob",
            importance=0.3,
        )
        self.store.add(first)
        self.store.add(second)

        loaded = self.store.get("one")
        self.assertEqual(loaded.content, first.content)
        self.assertEqual(loaded.metadata, first.metadata)
        self.assertEqual(
            [item.id for item in self.store.list_items(user_id="alice")],
            ["one"],
        )
        self.assertEqual(
            [
                item.id
                for item in self.store.list_items(min_importance=0.5)
            ],
            ["one"],
        )

        accessed_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        self.store.update_access(
            "one",
            accessed_at=accessed_at,
            access_count=3,
        )
        updated = self.store.get("one")
        self.assertEqual(updated.accessed_at, accessed_at)
        self.assertEqual(updated.access_count, 3)
        self.assertTrue(self.store.delete("one"))
        self.assertFalse(self.store.delete("missing"))

    def test_duplicate_invalid_metadata_and_closed_store_fail_safely(self):
        item = make_episode("one", "事件")
        self.store.add(item)
        with self.assertRaises(MemorySystemError):
            self.store.add(item)

        invalid = make_episode("invalid", "事件")
        invalid.metadata["bad"] = object()
        with self.assertRaises(MemorySystemError):
            self.store.add(invalid)

        self.store.close()
        with self.assertRaises(MemorySystemError):
            self.store.get("one")

    def test_clear_can_be_scoped_by_type_and_user(self):
        self.store.add(make_episode("alice", "Alice 的事件"))
        self.store.add(make_episode("bob", "Bob 的事件", user_id="bob"))
        working = MemoryItem(
            id="working",
            content="临时信息",
            metadata={"user_id": "alice"},
        )
        self.store.add(working)

        removed = self.store.clear(memory_type="episodic", user_id="alice")

        self.assertEqual(removed, 1)
        self.assertEqual(
            {item.id for item in self.store.list_items()},
            {"bob", "working"},
        )

    def test_file_database_persists_after_reopen(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            first_store = SQLiteMemoryStore(path)
            first_store.add(make_episode("persisted", "长期事件"))
            first_store.close()

            reopened = SQLiteMemoryStore(path)
            try:
                self.assertEqual(
                    reopened.get("persisted").content,
                    "长期事件",
                )
            finally:
                reopened.close()


class TestEpisodicMemory(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.embedder = HashingEmbedder(128)
        self.vector_store = InMemoryVectorStore(128)
        self.document_store = SQLiteMemoryStore(":memory:")
        self.memory = EpisodicMemory(
            document_store=self.document_store,
            vector_store=self.vector_store,
            embedder=self.embedder,
            user_id="alice",
            clock=self.clock,
        )

    def tearDown(self):
        self.document_store.close()

    def test_add_retrieve_and_access_tracking(self):
        self.memory.add(
            make_episode(
                "python",
                "用户完成了 Python 智能体项目",
                importance=0.8,
            )
        )
        self.memory.add(
            make_episode(
                "travel",
                "用户去上海旅行",
                importance=0.8,
            )
        )

        results = self.memory.retrieve("Python 项目", limit=2)

        self.assertEqual(results[0].item.id, "python")
        self.assertGreater(results[0].score, 0)
        self.assertEqual(results[0].item.access_count, 1)
        self.assertEqual(
            self.document_store.get("python").access_count,
            1,
        )

    def test_importance_filter_and_recency_affect_results(self):
        old_time = self.clock.now - timedelta(days=120)
        self.memory.add(
            make_episode(
                "old",
                "Python 项目里程碑",
                importance=0.9,
                created_at=old_time,
            )
        )
        self.memory.add(
            make_episode(
                "new",
                "Python 项目里程碑",
                importance=0.9,
                created_at=self.clock.now,
            )
        )
        self.memory.add(
            make_episode(
                "low",
                "Python 项目",
                importance=0.1,
            )
        )

        results = self.memory.retrieve(
            "Python 项目",
            limit=5,
            min_importance=0.5,
        )

        self.assertEqual([result.item.id for result in results], ["new", "old"])

    def test_rejects_other_users_and_wrong_memory_type(self):
        with self.assertRaises(MemorySystemError):
            self.memory.add(
                make_episode("bob", "Bob 的事件", user_id="bob")
            )
        with self.assertRaises(MemorySystemError):
            self.memory.add(MemoryItem(content="工作记忆"))

    def test_remove_and_clear_keep_vector_and_document_stores_consistent(self):
        self.memory.add(make_episode("one", "第一个事件"))
        self.memory.add(make_episode("two", "第二个事件"))

        removed = self.memory.remove("one")
        self.assertEqual(removed.id, "one")
        self.assertIsNone(self.document_store.get("one"))
        self.assertEqual(self.vector_store.count(), 1)
        self.assertEqual(self.memory.clear(), 1)
        self.assertEqual(len(self.memory), 0)
        self.assertEqual(self.vector_store.count(), 0)

    def test_vector_failure_rolls_back_document_write(self):
        failing = EpisodicMemory(
            document_store=self.document_store,
            vector_store=FailingVectorStore(128),
            embedder=self.embedder,
            user_id="alice",
        )

        with self.assertRaises(MemorySystemError):
            failing.add(make_episode("rollback", "应该回滚"))

        self.assertIsNone(self.document_store.get("rollback"))

    def test_constructor_and_retrieval_validation(self):
        with self.assertRaises(MemorySystemError):
            EpisodicMemory(
                document_store=self.document_store,
                vector_store=InMemoryVectorStore(64),
                embedder=self.embedder,
            )
        with self.assertRaises(MemorySystemError):
            self.memory.retrieve(" ", limit=1)
        with self.assertRaises(MemorySystemError):
            self.memory.retrieve("事件", limit=0)
        with self.assertRaises(MemorySystemError):
            self.memory.retrieve("事件", limit=1, min_importance=2)

    def test_two_users_share_backends_without_cross_retrieval_or_clear(self):
        bob_memory = EpisodicMemory(
            document_store=self.document_store,
            vector_store=self.vector_store,
            embedder=self.embedder,
            user_id="bob",
            clock=self.clock,
        )
        self.memory.add(make_episode("alice", "Alice Python 事件"))
        bob_memory.add(
            make_episode("bob", "Bob Python 事件", user_id="bob")
        )

        alice_results = self.memory.retrieve("Python", limit=5)
        self.assertEqual([result.item.id for result in alice_results], ["alice"])
        self.assertEqual(self.memory.clear(), 1)
        self.assertEqual([item.id for item in bob_memory.list_items()], ["bob"])
        self.assertEqual(self.vector_store.count(), 1)

    def test_manager_and_memory_tool_can_use_episodic_backend(self):
        manager = MemoryManager(
            user_id="alice",
            backends={"episodic": self.memory},
        )
        tool = MemoryTool(manager)

        added = tool.run(
            {
                "action": "add",
                "content": "完成了持久化记忆阶段",
                "memory_type": "episodic",
                "importance": 0.9,
            }
        )
        searched = tool.run(
            {
                "action": "search",
                "query": "持久化记忆",
                "memory_type": "episodic",
            }
        )

        self.assertIn("已添加 episodic 记忆", added)
        self.assertIn("完成了持久化记忆阶段", searched)
        self.assertEqual(manager.get_stats()["by_type"]["episodic"], 1)


if __name__ == "__main__":
    unittest.main()
