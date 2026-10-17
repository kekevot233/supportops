import unittest
from datetime import datetime, timedelta, timezone

from hello_agents import (
    HashingEmbedder,
    InMemoryVectorStore,
    MemoryConfig,
    MemoryItem,
    MemoryManager,
    MemorySystemError,
    MemoryTool,
    MetadataModalityEncoder,
    PerceptualMemory,
    SQLiteMemoryStore,
    TextModalityEncoder,
    ToolError,
    VectorStoreError,
    infer_modality,
    normalize_modality,
)


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


class FailingVectorStore(InMemoryVectorStore):
    def upsert(self, records):
        raise VectorStoreError("simulated failure")


def make_perception(
    memory_id,
    content,
    *,
    user_id="alice",
    importance=0.5,
    created_at=None,
    modality=None,
    raw_data=None,
):
    timestamp = created_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
    metadata = {"user_id": user_id}
    if modality is not None:
        metadata["modality"] = modality
    if raw_data is not None:
        metadata["raw_data"] = raw_data
    return MemoryItem(
        id=memory_id,
        content=content,
        memory_type="perceptual",
        importance=importance,
        created_at=timestamp,
        accessed_at=timestamp,
        metadata=metadata,
    )


class TestModalityUtilities(unittest.TestCase):
    def test_infers_common_extensions_without_reading_files(self):
        self.assertEqual(infer_modality("photo.JPG"), "image")
        self.assertEqual(infer_modality("voice.wav"), "audio")
        self.assertEqual(infer_modality("clip.mp4"), "video")
        self.assertEqual(infer_modality("notes.PDF"), "text")
        self.assertEqual(infer_modality("archive.bin"), "file")
        self.assertEqual(infer_modality(), "text")
        self.assertEqual(
            infer_modality("photo.jpg", explicit="CUSTOM_MODE"),
            "custom_mode",
        )

    def test_rejects_invalid_modality_and_paths(self):
        with self.assertRaises(MemorySystemError):
            normalize_modality("bad modality")
        with self.assertRaises(MemorySystemError):
            infer_modality(" ")
        with self.assertRaises(MemorySystemError):
            infer_modality(object())

    def test_text_and_metadata_encoders_enforce_boundaries(self):
        embedder = HashingEmbedder(64)
        text = TextModalityEncoder(embedder)
        image = MetadataModalityEncoder("image", embedder)
        item = make_perception("image", "架构图", raw_data="diagram.png")

        self.assertEqual(len(text.encode_query("架构")), 64)
        self.assertEqual(len(image.encode_item(item)), 64)
        with self.assertRaises(MemorySystemError):
            text.encode_query("声音", query_modality="audio")
        with self.assertRaises(MemorySystemError):
            image.encode_item(make_perception("bad", "无文件"))
        with self.assertRaises(MemorySystemError):
            MetadataModalityEncoder("text", embedder)


class TestPerceptualMemory(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.embedder = HashingEmbedder(128)
        self.text_store = InMemoryVectorStore(128)
        self.image_store = InMemoryVectorStore(128)
        self.audio_store = InMemoryVectorStore(128)
        self.document_store = SQLiteMemoryStore(":memory:")
        self.memory = PerceptualMemory(
            document_store=self.document_store,
            encoders={
                "text": TextModalityEncoder(self.embedder),
                "image": MetadataModalityEncoder("image", self.embedder),
                "audio": MetadataModalityEncoder("audio", self.embedder),
            },
            vector_stores={
                "text": self.text_store,
                "image": self.image_store,
                "audio": self.audio_store,
            },
            user_id="alice",
            clock=self.clock,
        )

    def tearDown(self):
        self.document_store.close()

    def test_add_infers_modality_and_separates_vector_spaces(self):
        self.memory.add(
            make_perception(
                "diagram",
                "Agent 框架架构图",
                raw_data=r"D:\materials\agent-architecture.PNG",
            )
        )
        self.memory.add(make_perception("note", "Agent 框架文字笔记"))

        stored = {item.id: item for item in self.memory.list_items()}
        self.assertEqual(stored["diagram"].metadata["modality"], "image")
        self.assertEqual(stored["note"].metadata["modality"], "text")
        self.assertEqual(self.image_store.count(), 1)
        self.assertEqual(self.text_store.count(), 1)
        self.assertEqual(self.audio_store.count(), 0)

    def test_target_modality_filters_results(self):
        self.memory.add(
            make_perception(
                "diagram",
                "Agent 系统架构",
                raw_data="agent-system.png",
            )
        )
        self.memory.add(make_perception("note", "Agent 系统架构"))

        image_results = self.memory.retrieve(
            "Agent 系统架构", limit=5, target_modality="image"
        )
        all_results = self.memory.retrieve("Agent 系统架构", limit=5)

        self.assertEqual([result.item.id for result in image_results], ["diagram"])
        self.assertEqual(
            {result.item.id for result in all_results}, {"diagram", "note"}
        )

    def test_recency_and_importance_affect_ranking(self):
        config = MemoryConfig(perceptual_recency_decay_hours=24)
        memory = PerceptualMemory(
            document_store=self.document_store,
            encoders={"text": TextModalityEncoder(self.embedder)},
            vector_stores={"text": self.text_store},
            user_id="alice",
            config=config,
            clock=self.clock,
        )
        memory.add(
            make_perception(
                "old",
                "相同的会议摘要",
                importance=0.8,
                created_at=self.clock.now - timedelta(days=30),
            )
        )
        memory.add(
            make_perception(
                "new",
                "相同的会议摘要",
                importance=0.8,
                created_at=self.clock.now,
            )
        )
        memory.add(
            make_perception(
                "low",
                "相同的会议摘要",
                importance=0.1,
                created_at=self.clock.now,
            )
        )

        results = memory.retrieve(
            "相同的会议摘要", limit=5, min_importance=0.5
        )

        self.assertEqual([result.item.id for result in results], ["new", "old"])

    def test_remove_and_clear_delete_only_owned_modality_vectors(self):
        bob = PerceptualMemory(
            document_store=self.document_store,
            encoders={
                "text": TextModalityEncoder(self.embedder),
                "image": MetadataModalityEncoder("image", self.embedder),
                "audio": MetadataModalityEncoder("audio", self.embedder),
            },
            vector_stores={
                "text": self.text_store,
                "image": self.image_store,
                "audio": self.audio_store,
            },
            user_id="bob",
            clock=self.clock,
        )
        self.memory.add(make_perception("alice-text", "Alice 文字"))
        self.memory.add(
            make_perception(
                "alice-image", "Alice 图片", raw_data="alice.png"
            )
        )
        bob.add(
            make_perception(
                "bob-image",
                "Bob 图片",
                user_id="bob",
                raw_data="bob.png",
            )
        )

        self.memory.remove("alice-text")
        self.assertEqual(self.text_store.count(), 0)
        self.assertEqual(self.memory.clear(), 1)

        self.assertEqual([item.id for item in bob.list_items()], ["bob-image"])
        self.assertEqual(self.image_store.count(), 1)

    def test_vector_failure_rolls_back_document(self):
        failing = PerceptualMemory(
            document_store=self.document_store,
            encoders={"text": TextModalityEncoder(self.embedder)},
            vector_stores={"text": FailingVectorStore(128)},
            user_id="alice",
        )

        with self.assertRaises(MemorySystemError):
            failing.add(make_perception("rollback", "应该回滚"))

        self.assertIsNone(self.document_store.get("rollback"))

    def test_constructor_and_input_validation(self):
        with self.assertRaises(MemorySystemError):
            PerceptualMemory(
                document_store=self.document_store,
                encoders={"text": TextModalityEncoder(self.embedder)},
                vector_stores={"image": self.image_store},
            )
        with self.assertRaises(MemorySystemError):
            PerceptualMemory(
                document_store=self.document_store,
                encoders={"text": TextModalityEncoder(self.embedder)},
                vector_stores={"text": InMemoryVectorStore(64)},
            )
        with self.assertRaises(MemorySystemError):
            self.memory.add(MemoryItem(content="普通记忆"))
        with self.assertRaises(MemorySystemError):
            self.memory.add(
                make_perception("bob", "Bob", user_id="bob")
            )
        with self.assertRaises(MemorySystemError):
            self.memory.add(
                make_perception("video", "视频", raw_data="demo.mp4")
            )
        with self.assertRaises(MemorySystemError):
            self.memory.retrieve("内容", limit=1, target_modality="video")
        with self.assertRaises(MemorySystemError):
            self.memory.retrieve(" ", limit=1)

    def test_manager_and_tool_accept_file_path_without_content(self):
        manager = MemoryManager(
            user_id="alice", backends={"perceptual": self.memory}
        )
        tool = MemoryTool(manager)

        added = tool.run(
            {
                "action": "add",
                "memory_type": "perceptual",
                "file_path": r"D:\materials\whiteboard.png",
                "metadata": {"scene": "架构讨论"},
            }
        )
        memory_id = added.split("ID：", 1)[1].removesuffix("。")
        stored = self.document_store.get(memory_id)

        self.assertEqual(stored.content, "感知文件：whiteboard.png")
        self.assertEqual(stored.metadata["modality"], "image")
        self.assertEqual(stored.metadata["scene"], "架构讨论")
        self.assertEqual(self.image_store.count(), 1)

        with self.assertRaises(ToolError):
            tool.run(
                {
                    "action": "add",
                    "memory_type": "working",
                    "content": "错误",
                    "file_path": "image.png",
                }
            )


if __name__ == "__main__":
    unittest.main()
