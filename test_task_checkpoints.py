import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from hello_agents import (
    ContextEngineeringError,
    NoteTool,
    TaskCheckpoint,
    TaskCheckpointStore,
)


class TestTaskCheckpointStore(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "notes"
        self.notes = NoteTool(self.workspace)
        self.store = TaskCheckpointStore(self.notes)

    def tearDown(self):
        self.temporary.cleanup()

    def test_create_and_reload_after_process_restart(self):
        created = self.store.create(
            "chapter9-demo",
            "完成第九章上下文工程",
            steps=["分析需求", "实现功能", "运行测试"],
            metadata={"chapter": 9},
        )

        reloaded = TaskCheckpointStore(NoteTool(self.workspace)).load(
            "chapter9-demo"
        )

        self.assertEqual(reloaded, created)
        self.assertEqual(reloaded.current_step, "分析需求")
        self.assertEqual(reloaded.pending_steps, ["实现功能", "运行测试"])
        self.assertEqual(reloaded.metadata, {"chapter": 9})

    def test_duplicate_task_is_rejected(self):
        self.store.create("same-task", "第一次创建")

        with self.assertRaisesRegex(ContextEngineeringError, "已经存在"):
            self.store.create("same-task", "重复创建")

    def test_completing_steps_advances_and_reaches_terminal_state(self):
        checkpoint = self.store.create(
            "step-flow", "验证状态推进", steps=["步骤一", "步骤二"]
        )

        checkpoint = self.store.mark_current_step_completed(
            "step-flow", expected_version=checkpoint.version
        )
        self.assertEqual(checkpoint.status, "running")
        self.assertEqual(checkpoint.current_step, "步骤二")
        self.assertEqual(checkpoint.completed_steps, ["步骤一"])
        self.assertEqual(checkpoint.version, 2)

        checkpoint = self.store.mark_current_step_completed(
            "step-flow", expected_version=checkpoint.version
        )
        self.assertEqual(checkpoint.status, "completed")
        self.assertIsNone(checkpoint.current_step)
        self.assertEqual(checkpoint.completed_steps, ["步骤一", "步骤二"])
        self.assertFalse(self.store.plan_resume("step-flow").can_resume)

    def test_blocked_task_requires_resolution_before_resume(self):
        checkpoint = self.store.create(
            "blocked-flow", "验证阻塞恢复", steps=["等待数据库", "执行迁移"]
        )
        checkpoint = self.store.block(
            "blocked-flow", ["数据库未启动"], expected_version=checkpoint.version
        )

        plan = self.store.plan_resume("blocked-flow")
        self.assertFalse(plan.can_resume)
        self.assertEqual(plan.next_step, "等待数据库")
        self.assertEqual(plan.blockers, ["数据库未启动"])

        checkpoint = self.store.resume_blocked(
            "blocked-flow", expected_version=checkpoint.version
        )
        plan = self.store.plan_resume("blocked-flow")
        self.assertEqual(checkpoint.status, "running")
        self.assertTrue(plan.can_resume)
        self.assertEqual(plan.next_step, "等待数据库")

    def test_stale_checkpoint_cannot_overwrite_newer_version(self):
        stale = self.store.create("versioned", "验证乐观并发", steps=["A", "B"])
        current = self.store.mark_current_step_completed(
            "versioned", expected_version=stale.version
        )
        stale.status = "running"

        with self.assertRaisesRegex(ContextEngineeringError, "版本冲突"):
            self.store.save(stale, expected_version=stale.version)

        self.assertEqual(self.store.load("versioned"), current)

    def test_terminal_state_cannot_be_reopened(self):
        checkpoint = self.store.create("terminal", "验证终态", steps=["唯一步骤"])
        checkpoint = self.store.mark_current_step_completed(
            "terminal", expected_version=checkpoint.version
        )
        reopened = checkpoint.model_copy(update={"status": "failed"})

        with self.assertRaisesRegex(ContextEngineeringError, "不允许"):
            self.store.save(reopened, expected_version=checkpoint.version)

    def test_tampered_checkpoint_is_rejected(self):
        self.store.create("tampered", "验证摘要", steps=["安全恢复"])
        note = self.notes.list_notes(tags=["task:tampered"])[0]
        record = self.notes.read_note(note["id"])
        self.notes.update_note(
            note["id"],
            content=record.content.replace("安全恢复", "跳过校验", 1),
        )

        with self.assertRaisesRegex(ContextEngineeringError, "校验失败"):
            self.store.load("tampered")

    def test_list_tasks_returns_bounded_summary(self):
        self.store.create("task-a", "任务 A", steps=["A1"])
        self.store.create("task-b", "任务 B", steps=["B1"])

        tasks = self.store.list_tasks(limit=10)

        self.assertEqual({item["task_id"] for item in tasks}, {"task-a", "task-b"})
        self.assertTrue(all("metadata" not in item for item in tasks))
        with self.assertRaises(ContextEngineeringError):
            self.store.list_tasks(limit=0)


class TestTaskCheckpointValidation(unittest.TestCase):
    def test_step_text_is_trimmed_and_empty_step_is_rejected(self):
        checkpoint = TaskCheckpoint(
            task_id="trimmed", objective="目标", current_step="  执行测试  "
        )
        self.assertEqual(checkpoint.current_step, "执行测试")

        with self.assertRaises(ValidationError):
            TaskCheckpoint(task_id="empty-step", objective="目标", current_step="  ")

    def test_metadata_must_be_json_serializable(self):
        with self.assertRaises(ValidationError):
            TaskCheckpoint(
                task_id="bad-metadata",
                objective="目标",
                metadata={"unsafe": object()},
            )


if __name__ == "__main__":
    unittest.main()
