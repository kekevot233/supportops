import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from hello_agents import (
    EpisodicMemory,
    ForgettingPolicy,
    HashingEmbedder,
    InMemoryVectorStore,
    LifecycleStep,
    LifecycleTask,
    MemoryConfig,
    MemoryItem,
    MemoryManager,
    MemorySystemError,
    MemoryTool,
    SQLiteLifecycleStore,
    SQLiteMemoryStore,
    WorkingMemory,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FailOnceRemoveWorkingMemory(WorkingMemory):
    def __init__(self, config):
        super().__init__(config, clock=lambda: NOW)
        self.fail_next_remove = True

    def remove(self, memory_id):
        if self.fail_next_remove:
            self.fail_next_remove = False
            raise MemorySystemError("secret remove failure")
        return super().remove(memory_id)


def working_item(memory_id, importance=0.9):
    return MemoryItem(
        id=memory_id,
        content=f"记忆 {memory_id}",
        importance=importance,
        created_at=NOW,
        accessed_at=NOW,
        metadata={"user_id": "alice"},
    )


class TestSQLiteLifecycleStore(unittest.TestCase):
    def test_persists_task_steps_and_result_across_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lifecycle.sqlite3"
            store = SQLiteLifecycleStore(path)
            task = LifecycleTask(
                id="task-1",
                idempotency_key="request-1",
                user_id="alice",
                operation="forget",
                request={"strategy": "importance"},
                steps=[LifecycleStep(key="working:low", payload={"id": "low"})],
                created_at=NOW,
                updated_at=NOW,
            )
            store.create(task)
            task.status = "completed"
            task.steps[0].status = "completed"
            task.result = {"removed": []}
            store.save(task)
            store.close()

            reopened = SQLiteLifecycleStore(path)
            loaded = reopened.get_by_key("alice", "request-1")
            reopened.close()

        self.assertEqual(loaded.status, "completed")
        self.assertEqual(loaded.steps[0].status, "completed")
        self.assertEqual(loaded.result, {"removed": []})

    def test_unique_keys_user_isolation_and_closed_store(self):
        store = SQLiteLifecycleStore(":memory:")
        task = LifecycleTask(
            id="task-1",
            idempotency_key="same-key",
            user_id="alice",
            operation="forget",
            request={},
            created_at=NOW,
            updated_at=NOW,
        )
        store.create(task)
        with self.assertRaises(MemorySystemError):
            store.create(task.model_copy(update={"id": "task-2"}))
        bob = task.model_copy(
            update={"id": "task-3", "user_id": "bob"}, deep=True
        )
        store.create(bob)
        self.assertEqual(len(store.list_tasks(user_id="alice")), 1)
        self.assertEqual(len(store.list_tasks(user_id="bob")), 1)
        store.close()
        with self.assertRaises(MemorySystemError):
            store.list_tasks(user_id="alice")


class TestLifecycleRecovery(unittest.TestCase):
    def setUp(self):
        self.config = MemoryConfig(
            working_memory_capacity=20,
            working_memory_ttl_seconds=86400,
        )
        self.source = WorkingMemory(self.config, clock=lambda: NOW)
        self.documents = SQLiteMemoryStore(":memory:")
        self.target = EpisodicMemory(
            document_store=self.documents,
            vector_store=InMemoryVectorStore(64),
            embedder=HashingEmbedder(64),
            user_id="alice",
            clock=lambda: NOW,
        )
        self.journal = SQLiteLifecycleStore(":memory:")
        self.manager = MemoryManager(
            user_id="alice",
            config=self.config,
            backends={"working": self.source, "episodic": self.target},
            lifecycle_store=self.journal,
            clock=lambda: NOW,
        )

    def tearDown(self):
        self.documents.close()
        self.journal.close()

    def test_completed_consolidation_replays_same_result_without_duplicates(self):
        self.source.add(working_item("source"))

        first = self.manager.consolidate_memories(
            source_type="working",
            target_type="episodic",
            idempotency_key="promote-source",
        )
        second = self.manager.consolidate_memories(
            source_type="working",
            target_type="episodic",
            idempotency_key="promote-source",
        )

        self.assertEqual(first, second)
        self.assertEqual(len(self.source), 0)
        self.assertEqual(len(self.target), 1)
        task = self.manager.list_lifecycle_tasks()[0]
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.attempts, 1)
        self.assertEqual(task.steps[0].status, "completed")

    def test_failed_source_delete_is_repaired_without_duplicate_target(self):
        failing = FailOnceRemoveWorkingMemory(self.config)
        failing.add(working_item("source"))
        manager = MemoryManager(
            user_id="alice",
            backends={"working": failing, "episodic": self.target},
            lifecycle_store=self.journal,
            clock=lambda: NOW,
        )

        with self.assertRaises(MemorySystemError) as caught:
            manager.consolidate_memories(
                source_type="working",
                target_type="episodic",
                idempotency_key="recover-source-delete",
            )

        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(len(failing), 1)
        self.assertEqual(len(self.target), 1)
        failed = manager.list_lifecycle_tasks()[0]
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.steps[0].status, "target_written")

        reconciled = manager.reconcile_lifecycle_tasks()

        self.assertEqual(reconciled[0].status, "completed")
        self.assertEqual(reconciled[0].attempts, 2)
        self.assertEqual(len(failing), 0)
        self.assertEqual(len(self.target), 1)

    def test_reconciliation_rejects_target_id_owned_by_other_data(self):
        failing = FailOnceRemoveWorkingMemory(self.config)
        failing.add(working_item("source"))
        manager = MemoryManager(
            user_id="alice",
            backends={"working": failing, "episodic": self.target},
            lifecycle_store=self.journal,
            clock=lambda: NOW,
        )
        with self.assertRaises(MemorySystemError):
            manager.consolidate_memories(
                source_type="working",
                target_type="episodic",
                idempotency_key="conflicting-target",
            )
        task = manager.list_lifecycle_tasks()[0]
        target_id = task.steps[0].payload["target_id"]
        self.target.remove(target_id)
        self.target.add(
            MemoryItem(
                id=target_id,
                content="不属于该任务的目标",
                memory_type="episodic",
                importance=0.9,
                created_at=NOW,
                accessed_at=NOW,
                metadata={"user_id": "alice"},
            )
        )

        reconciled = manager.reconcile_lifecycle_tasks()

        self.assertEqual(reconciled[0].status, "failed")
        self.assertEqual(
            reconciled[0].steps[0].error_code,
            "target_identity_conflict",
        )
        self.assertEqual(len(failing), 1)
        self.assertEqual(len(self.target), 1)

    def test_failed_forget_is_reconciled_and_replay_returns_snapshot(self):
        failing = FailOnceRemoveWorkingMemory(self.config)
        failing.add(working_item("low", importance=0.1))
        manager = MemoryManager(
            user_id="alice",
            backends={"working": failing},
            lifecycle_store=self.journal,
            clock=lambda: NOW,
        )
        policy = ForgettingPolicy(
            strategy="importance", importance_threshold=0.2
        )
        with self.assertRaises(MemorySystemError):
            manager.forget_memories(
                policy,
                idempotency_key="forget-low",
            )

        manager.reconcile_lifecycle_tasks()
        replay = manager.forget_memories(
            policy,
            idempotency_key="forget-low",
        )

        self.assertEqual([item.id for item in replay], ["low"])
        self.assertEqual(len(failing), 0)
        self.assertEqual(manager.list_lifecycle_tasks()[0].status, "completed")

    def test_idempotency_key_rejects_different_request(self):
        self.source.add(working_item("source"))
        self.manager.consolidate_memories(
            source_type="working",
            target_type="episodic",
            min_importance=0.8,
            idempotency_key="same-key",
        )

        with self.assertRaisesRegex(MemorySystemError, "不同"):
            self.manager.consolidate_memories(
                source_type="working",
                target_type="episodic",
                min_importance=0.7,
                idempotency_key="same-key",
            )

    def test_idempotency_key_requires_store_and_validates_length(self):
        manager = MemoryManager(
            user_id="alice",
            backends={"working": self.source},
            clock=lambda: NOW,
        )
        policy = ForgettingPolicy(strategy="importance")
        with self.assertRaises(MemorySystemError):
            manager.forget_memories(policy, idempotency_key="key")
        with self.assertRaises(MemorySystemError):
            self.manager.forget_memories(policy, idempotency_key=" ")
        with self.assertRaises(MemorySystemError):
            self.manager.reconcile_lifecycle_tasks(limit=0)

    def test_empty_operation_is_logged_and_completed(self):
        result = self.manager.consolidate_memories(
            source_type="working",
            target_type="episodic",
            idempotency_key="nothing-to-do",
        )

        self.assertEqual(result, {})
        task = self.manager.list_lifecycle_tasks()[0]
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.steps, [])

    def test_memory_tool_replays_idempotent_consolidation(self):
        self.source.add(working_item("source"))
        tool = MemoryTool(self.manager)
        parameters = {
            "action": "consolidate",
            "memory_type": "working",
            "target_memory_type": "episodic",
            "idempotency_key": "tool-promote-source",
        }

        first = tool.run(parameters)
        second = tool.run(parameters)

        self.assertEqual(first, second)
        self.assertEqual(len(self.source), 0)
        self.assertEqual(len(self.target), 1)
        self.assertEqual(len(self.manager.list_lifecycle_tasks()), 1)

    def test_memory_tool_reports_and_reconciles_without_memory_content(self):
        failing = FailOnceRemoveWorkingMemory(self.config)
        failing.add(working_item("source"))
        manager = MemoryManager(
            user_id="alice",
            backends={"working": failing, "episodic": self.target},
            lifecycle_store=self.journal,
            clock=lambda: NOW,
        )
        with self.assertRaises(MemorySystemError):
            manager.consolidate_memories(
                source_type="working",
                target_type="episodic",
                idempotency_key="tool-reconcile",
            )
        tool = MemoryTool(manager)

        before = json.loads(tool.run({"action": "lifecycle_status"}))
        repaired = json.loads(tool.run({"action": "reconcile"}))
        after_text = tool.run({"action": "lifecycle_status"})
        after = json.loads(after_text)

        self.assertEqual(before["by_status"], {"failed": 1})
        self.assertEqual(repaired["by_status"], {"completed": 1})
        self.assertEqual(after["by_status"], {"completed": 1})
        self.assertNotIn("记忆 source", after_text)


if __name__ == "__main__":
    unittest.main()
