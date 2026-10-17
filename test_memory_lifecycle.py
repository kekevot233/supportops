import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from hello_agents import (
    EpisodicMemory,
    ForgettingPolicy,
    HashingEmbedder,
    InMemoryVectorStore,
    MemoryConfig,
    MemoryItem,
    MemoryManager,
    MemorySystemError,
    MemoryTool,
    SQLiteMemoryStore,
    WorkingMemory,
)


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class FailingRemoveWorkingMemory(WorkingMemory):
    def remove(self, memory_id):
        raise MemorySystemError("simulated source removal failure")


def make_working(memory_id, *, importance, created_at, access_count=0):
    return MemoryItem(
        id=memory_id,
        content=f"记忆 {memory_id}",
        importance=importance,
        created_at=created_at,
        accessed_at=created_at,
        access_count=access_count,
        metadata={"user_id": "alice"},
    )


class TestForgettingPolicy(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.items = [
            make_working(
                "old-low",
                importance=0.1,
                created_at=self.now - timedelta(days=120),
            ),
            make_working(
                "new-low",
                importance=0.1,
                created_at=self.now,
            ),
            make_working(
                "old-high",
                importance=0.9,
                created_at=self.now - timedelta(days=120),
                access_count=10,
            ),
        ]

    def test_importance_time_and_capacity_strategies(self):
        importance = ForgettingPolicy(
            strategy="importance", importance_threshold=0.2
        )
        time = ForgettingPolicy(strategy="time", max_age_days=90)
        capacity = ForgettingPolicy(strategy="capacity", capacity=2)

        self.assertEqual(
            {item.id for item in importance.select(self.items, now=self.now)},
            {"old-low", "new-low"},
        )
        self.assertEqual(
            {item.id for item in time.select(self.items, now=self.now)},
            {"old-low", "old-high"},
        )
        self.assertEqual(len(capacity.select(self.items, now=self.now)), 1)

    def test_smart_strategy_and_max_removals(self):
        policy = ForgettingPolicy(
            strategy="smart",
            max_age_days=30,
            retention_threshold=0.7,
            max_removals=1,
        )

        selected = policy.select(self.items, now=self.now)

        self.assertEqual([item.id for item in selected], ["old-low"])

    def test_policy_rejects_bad_weights_and_naive_time(self):
        with self.assertRaises(ValidationError):
            ForgettingPolicy(
                strategy="smart",
                importance_weight=0.5,
                recency_weight=0.5,
                access_weight=0.5,
            )
        with self.assertRaises(ValueError):
            ForgettingPolicy(strategy="time").select(
                self.items, now=datetime(2026, 1, 1)
            )


class TestMemoryLifecycleManager(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.config = MemoryConfig(
            working_memory_capacity=20,
            working_memory_ttl_seconds=365 * 86400,
        )
        self.working = WorkingMemory(self.config, clock=self.clock)
        self.documents = SQLiteMemoryStore(":memory:")
        self.episodic = EpisodicMemory(
            document_store=self.documents,
            vector_store=InMemoryVectorStore(64),
            embedder=HashingEmbedder(64),
            user_id="alice",
            clock=self.clock,
        )
        self.manager = MemoryManager(
            user_id="alice",
            config=self.config,
            backends={"working": self.working, "episodic": self.episodic},
            clock=self.clock,
        )

    def tearDown(self):
        self.documents.close()

    def test_forget_uses_backend_remove_and_can_scope_type(self):
        self.working.add(
            make_working(
                "low", importance=0.1, created_at=self.clock.now
            )
        )
        self.working.add(
            make_working(
                "high", importance=0.9, created_at=self.clock.now
            )
        )
        self.manager.add_memory(
            "低重要性事件", memory_type="episodic", importance=0.1
        )

        removed = self.manager.forget_memories(
            ForgettingPolicy(
                strategy="importance", importance_threshold=0.2
            ),
            memory_types=["working"],
        )

        self.assertEqual([item.id for item in removed], ["low"])
        self.assertEqual([item.id for item in self.working.list_items()], ["high"])
        self.assertEqual(len(self.episodic), 1)

    def test_consolidates_high_value_memory_with_provenance(self):
        self.working.add(
            make_working(
                "promote", importance=0.9, created_at=self.clock.now
            )
        )
        self.working.add(
            make_working(
                "keep", importance=0.4, created_at=self.clock.now
            )
        )

        migrated = self.manager.consolidate_memories(
            source_type="working",
            target_type="episodic",
            min_importance=0.8,
        )

        self.assertEqual(set(migrated), {"promote"})
        self.assertEqual([item.id for item in self.working.list_items()], ["keep"])
        promoted = self.episodic.list_items()[0]
        self.assertEqual(promoted.metadata["consolidated_from"], "promote")
        self.assertEqual(
            promoted.metadata["consolidated_from_type"], "working"
        )
        self.assertNotEqual(promoted.id, "promote")

    def test_consolidation_rolls_back_target_if_source_removal_fails(self):
        failing_source = FailingRemoveWorkingMemory(
            self.config, clock=self.clock
        )
        failing_source.add(
            make_working(
                "source", importance=0.9, created_at=self.clock.now
            )
        )
        manager = MemoryManager(
            user_id="alice",
            backends={"working": failing_source, "episodic": self.episodic},
            clock=self.clock,
        )

        with self.assertRaises(MemorySystemError):
            manager.consolidate_memories(
                source_type="working", target_type="episodic"
            )

        self.assertEqual(len(failing_source), 1)
        self.assertEqual(len(self.episodic), 0)

    def test_lifecycle_validation(self):
        with self.assertRaises(MemorySystemError):
            self.manager.consolidate_memories(
                source_type="working", target_type="working"
            )
        with self.assertRaises(MemorySystemError):
            self.manager.consolidate_memories(
                source_type="working",
                target_type="episodic",
                min_importance=2,
            )
        invalid_clock = MemoryManager(
            backends={"working": self.working},
            clock=lambda: datetime(2026, 1, 1),
        )
        with self.assertRaises(MemorySystemError):
            invalid_clock.forget_memories(
                ForgettingPolicy(strategy="importance")
            )

    def test_memory_tool_exposes_forget_and_consolidate(self):
        self.working.add(
            make_working(
                "low", importance=0.1, created_at=self.clock.now
            )
        )
        self.working.add(
            make_working(
                "high", importance=0.9, created_at=self.clock.now
            )
        )
        tool = MemoryTool(self.manager)

        forgotten = tool.run(
            {
                "action": "forget",
                "memory_type": "working",
                "strategy": "importance",
                "threshold": 0.2,
            }
        )
        consolidated = tool.run(
            {
                "action": "consolidate",
                "memory_type": "working",
                "target_memory_type": "episodic",
                "consolidation_threshold": 0.8,
                "limit": 5,
            }
        )

        self.assertIn("遗忘 1 条", forgotten)
        self.assertIn("整合为 episodic", consolidated)
        self.assertEqual(len(self.working), 0)
        self.assertEqual(len(self.episodic), 1)


if __name__ == "__main__":
    unittest.main()
