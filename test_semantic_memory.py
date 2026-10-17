import unittest

from hello_agents import (
    GraphStoreError,
    HashingEmbedder,
    InMemoryGraphStore,
    InMemoryVectorStore,
    MemoryItem,
    MemoryManager,
    MemorySystemError,
    MemoryTool,
    MetadataKnowledgeExtractor,
    SemanticMemory,
    SQLiteMemoryStore,
)


def make_semantic(
    memory_id,
    content,
    *,
    user_id="alice",
    importance=0.5,
    entities=None,
    relations=None,
):
    metadata = {"user_id": user_id}
    if entities is not None:
        metadata["entities"] = entities
    if relations is not None:
        metadata["relations"] = relations
    return MemoryItem(
        id=memory_id,
        content=content,
        memory_type="semantic",
        importance=importance,
        metadata=metadata,
    )


class FailingGraphStore(InMemoryGraphStore):
    def upsert(self, entities, relations):
        raise GraphStoreError("simulated failure")


class TestMetadataKnowledgeExtractor(unittest.TestCase):
    def setUp(self):
        self.extractor = MetadataKnowledgeExtractor()

    def test_extracts_entities_relations_and_stable_ids(self):
        item = make_semantic(
            "memory-1",
            "Python 可用于构建 Agent",
            entities=[
                {"name": "Python", "type": "Technology"},
                "Agent",
            ],
            relations=[
                {
                    "source": "Python",
                    "target": "Agent",
                    "type": "用于构建",
                    "properties": {"confidence": 0.9},
                }
            ],
        )

        first = self.extractor.extract(item, user_id="alice")
        second = self.extractor.extract(item, user_id="alice")

        self.assertEqual(len(first.entities), 2)
        self.assertEqual(len(first.relations), 1)
        self.assertEqual(first.entities[0].id, second.entities[0].id)
        relation = first.relations[0]
        self.assertEqual(relation.relation_type, "用于构建")
        self.assertEqual(relation.source_memory_id, "memory-1")
        self.assertEqual(relation.properties["confidence"], 0.9)

    def test_relation_can_create_missing_concept_entities(self):
        item = make_semantic(
            "memory-1",
            "Neo4j 是图数据库",
            relations=[
                {"source": "Neo4j", "target": "图数据库", "type": "属于"}
            ],
        )

        knowledge = self.extractor.extract(item, user_id="alice")

        self.assertEqual(
            {entity.name for entity in knowledge.entities},
            {"Neo4j", "图数据库"},
        )

    def test_rejects_invalid_metadata_shape(self):
        item = make_semantic("memory-1", "知识", entities="Python")
        with self.assertRaises(MemorySystemError):
            self.extractor.extract(item, user_id="alice")

        item = make_semantic(
            "memory-2",
            "知识",
            relations=[{"source": "A", "target": "B"}],
        )
        with self.assertRaises(MemorySystemError):
            self.extractor.extract(item, user_id="alice")


class TestSemanticMemory(unittest.TestCase):
    def setUp(self):
        self.embedder = HashingEmbedder(128)
        self.vector_store = InMemoryVectorStore(128)
        self.graph_store = InMemoryGraphStore()
        self.document_store = SQLiteMemoryStore(":memory:")
        self.memory = SemanticMemory(
            document_store=self.document_store,
            vector_store=self.vector_store,
            graph_store=self.graph_store,
            embedder=self.embedder,
            user_id="alice",
        )

    def tearDown(self):
        self.document_store.close()

    def test_add_builds_three_indexes_and_retrieves_by_relation(self):
        self.memory.add(
            make_semantic(
                "python-agent",
                "Python 是构建智能体的常用语言",
                importance=0.9,
                entities=[
                    {"name": "Python", "type": "Technology"},
                    {"name": "Agent", "type": "Concept"},
                ],
                relations=[
                    {
                        "source": "Python",
                        "target": "Agent",
                        "type": "用于构建",
                    }
                ],
            )
        )
        self.memory.add(
            make_semantic(
                "weather",
                "天气由大气状态决定",
                entities=["天气"],
            )
        )

        results = self.memory.retrieve("用于构建", limit=2)

        self.assertEqual(results[0].item.id, "python-agent")
        self.assertEqual(results[0].item.access_count, 1)
        self.assertEqual(len(self.memory), 2)
        self.assertEqual(self.vector_store.count(), 2)
        self.assertEqual(
            self.graph_store.stats("alice"), {"entities": 3, "relations": 1}
        )

    def test_vector_only_memory_and_importance_filter(self):
        self.memory.add(
            make_semantic(
                "high",
                "Qdrant 提供向量检索",
                importance=0.9,
            )
        )
        self.memory.add(
            make_semantic(
                "low",
                "Qdrant 是数据库",
                importance=0.1,
            )
        )

        results = self.memory.retrieve(
            "Qdrant 向量检索", limit=5, min_importance=0.5
        )

        self.assertEqual([result.item.id for result in results], ["high"])

    def test_shared_entity_and_remove_keep_remaining_source(self):
        self.memory.add(
            make_semantic("one", "Python 知识一", entities=["Python"])
        )
        self.memory.add(
            make_semantic("two", "Python 知识二", entities=["Python"])
        )

        self.memory.remove("one")

        self.assertIsNone(self.document_store.get("one"))
        self.assertEqual(self.vector_store.count(), 1)
        self.assertEqual(self.graph_store.stats("alice")["entities"], 1)
        self.assertEqual(
            self.graph_store.search("Python", user_id="alice", limit=5)[0]
            .memory_id,
            "two",
        )

    def test_two_users_share_backends_without_cross_access_or_clear(self):
        bob = SemanticMemory(
            document_store=self.document_store,
            vector_store=self.vector_store,
            graph_store=self.graph_store,
            embedder=self.embedder,
            user_id="bob",
        )
        self.memory.add(
            make_semantic("alice", "Alice 使用 Python", entities=["Python"])
        )
        bob.add(
            make_semantic(
                "bob",
                "Bob 使用 Python",
                user_id="bob",
                entities=["Python"],
            )
        )

        self.assertEqual(
            [item.item.id for item in self.memory.retrieve("Python", limit=5)],
            ["alice"],
        )
        self.assertEqual(self.memory.clear(), 1)
        self.assertEqual([item.id for item in bob.list_items()], ["bob"])
        self.assertEqual(self.graph_store.stats("bob")["entities"], 1)
        self.assertEqual(self.vector_store.count(), 1)

    def test_graph_failure_rolls_back_document_and_vector(self):
        failing = SemanticMemory(
            document_store=self.document_store,
            vector_store=self.vector_store,
            graph_store=FailingGraphStore(),
            embedder=self.embedder,
            user_id="alice",
        )

        with self.assertRaises(MemorySystemError):
            failing.add(
                make_semantic("rollback", "需要回滚", entities=["实体"])
            )

        self.assertIsNone(self.document_store.get("rollback"))
        self.assertEqual(self.vector_store.count(), 0)

    def test_rejects_wrong_type_user_dimension_and_retrieval_values(self):
        with self.assertRaises(MemorySystemError):
            self.memory.add(MemoryItem(content="临时信息"))
        with self.assertRaises(MemorySystemError):
            self.memory.add(
                make_semantic("bob", "Bob", user_id="bob")
            )
        with self.assertRaises(MemorySystemError):
            SemanticMemory(
                document_store=self.document_store,
                vector_store=InMemoryVectorStore(64),
                graph_store=self.graph_store,
                embedder=self.embedder,
            )
        with self.assertRaises(MemorySystemError):
            self.memory.retrieve(" ", limit=1)
        with self.assertRaises(MemorySystemError):
            self.memory.retrieve("知识", limit=0)
        with self.assertRaises(MemorySystemError):
            self.memory.retrieve("知识", limit=1, min_importance=2)

    def test_manager_and_tool_route_semantic_backend(self):
        manager = MemoryManager(
            user_id="alice", backends={"semantic": self.memory}
        )
        tool = MemoryTool(manager)

        added = tool.run(
            {
                "action": "add",
                "content": "Qdrant 是向量数据库",
                "memory_type": "semantic",
                "importance": 0.8,
                "metadata": {
                    "entities": ["Qdrant", "向量数据库"],
                    "relations": [
                        {
                            "source": "Qdrant",
                            "target": "向量数据库",
                            "type": "属于",
                        }
                    ],
                },
            }
        )
        searched = tool.run(
            {
                "action": "search",
                "query": "Qdrant 向量",
                "memory_type": "semantic",
            }
        )

        self.assertIn("已添加 semantic 记忆", added)
        self.assertIn("Qdrant 是向量数据库", searched)
        self.assertEqual(manager.get_stats()["by_type"]["semantic"], 1)
        self.assertEqual(
            self.graph_store.stats("alice"), {"entities": 2, "relations": 1}
        )


if __name__ == "__main__":
    unittest.main()
