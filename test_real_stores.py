"""需要显式开关和本机服务的 Qdrant/Neo4j 真实兼容性测试。"""

from __future__ import annotations

import os
import unittest
from uuid import uuid4

from hello_agents import (
    EpisodicMemory,
    GraphEntity,
    GraphRelation,
    HashingEmbedder,
    MemoryItem,
    MemoryManager,
    Neo4jGraphStore,
    QdrantVectorStore,
    SemanticMemory,
    SQLiteMemoryStore,
    VectorRecord,
)


RUN_REAL_STORES = os.getenv("HELLO_AGENTS_RUN_REAL_STORES") == "1"


@unittest.skipUnless(
    RUN_REAL_STORES,
    "设置 HELLO_AGENTS_RUN_REAL_STORES=1 后才运行真实存储测试。",
)
class TestRealQdrantVectorStore(unittest.TestCase):
    def setUp(self):
        from qdrant_client import QdrantClient

        url = os.getenv("HELLO_AGENTS_TEST_QDRANT_URL", "http://127.0.0.1:6333")
        self.collection = f"hello_agents_it_{uuid4().hex}"
        self.client = QdrantClient(url=url)
        self.store = QdrantVectorStore(
            collection_name=self.collection,
            dimension=3,
            client=self.client,
        )

    def tearDown(self):
        try:
            if self.client.collection_exists(self.collection):
                self.client.delete_collection(self.collection)
        finally:
            self.client.close()

    def test_real_upsert_filter_search_delete_clear_and_count(self):
        self.assertEqual(
            self.store.upsert(
                [
                    VectorRecord(
                        id="alice-primary",
                        vector=[1.0, 0.0, 0.0],
                        metadata={"user_id": "alice", "kind": "note"},
                    ),
                    VectorRecord(
                        id="alice-secondary",
                        vector=[0.0, 1.0, 0.0],
                        metadata={"user_id": "alice", "kind": "note"},
                    ),
                    VectorRecord(
                        id="bob-primary",
                        vector=[1.0, 0.0, 0.0],
                        metadata={"user_id": "bob", "kind": "note"},
                    ),
                ]
            ),
            3,
        )
        results = self.store.search(
            [1.0, 0.0, 0.0],
            limit=5,
            filters={"user_id": "alice", "kind": "note"},
        )

        self.assertEqual([result.id for result in results], [
            "alice-primary",
            "alice-secondary",
        ])
        self.assertEqual(self.store.count(), 3)

        self.store.delete(["alice-primary"])
        self.assertEqual(self.store.count(), 2)
        self.store.clear()
        self.assertEqual(self.store.count(), 0)


@unittest.skipUnless(
    RUN_REAL_STORES,
    "设置 HELLO_AGENTS_RUN_REAL_STORES=1 后才运行真实存储测试。",
)
class TestRealNeo4jGraphStore(unittest.TestCase):
    def setUp(self):
        suffix = uuid4().hex
        self.alice = f"alice-{suffix}"
        self.bob = f"bob-{suffix}"
        self.store = Neo4jGraphStore(
            uri=os.getenv(
                "HELLO_AGENTS_TEST_NEO4J_URI",
                "neo4j://127.0.0.1:7687",
            ),
            username=os.getenv("HELLO_AGENTS_TEST_NEO4J_USERNAME", "neo4j"),
            password=os.environ["HELLO_AGENTS_TEST_NEO4J_PASSWORD"],
        )

    def tearDown(self):
        try:
            self.store.clear_user(self.alice)
            self.store.clear_user(self.bob)
        finally:
            self.store.close()

    def test_real_shared_entity_relation_search_delete_and_isolation(self):
        memory_id = f"memory-{uuid4().hex}"
        shared_memory_id = f"memory-{uuid4().hex}"
        python_id = f"entity-{uuid4().hex}"
        agent_id = f"entity-{uuid4().hex}"
        relation_id = f"relation-{uuid4().hex}"
        self.store.upsert(
            [
                GraphEntity(
                    id=python_id,
                    name="Python",
                    entity_type="Technology",
                    user_id=self.alice,
                    source_memory_ids=[memory_id],
                ),
                GraphEntity(
                    id=agent_id,
                    name="Agent Framework",
                    entity_type="Project",
                    user_id=self.alice,
                    source_memory_ids=[memory_id],
                ),
            ],
            [
                GraphRelation(
                    id=relation_id,
                    source_id=agent_id,
                    target_id=python_id,
                    relation_type="USES",
                    user_id=self.alice,
                    source_memory_id=memory_id,
                )
            ],
        )
        self.store.upsert(
            [
                GraphEntity(
                    id=python_id,
                    name="Python",
                    entity_type="Technology",
                    user_id=self.alice,
                    source_memory_ids=[shared_memory_id],
                ),
                GraphEntity(
                    id=f"entity-{uuid4().hex}",
                    name="Python",
                    entity_type="Technology",
                    user_id=self.bob,
                    source_memory_ids=[f"memory-{uuid4().hex}"],
                ),
            ],
            [],
        )

        results = self.store.search("uses Python", user_id=self.alice, limit=5)

        self.assertEqual(results[0].memory_id, memory_id)
        self.assertEqual(self.store.stats(self.alice), {
            "entities": 2,
            "relations": 1,
        })
        self.assertEqual(self.store.stats(self.bob), {
            "entities": 1,
            "relations": 0,
        })

        self.store.delete_memory(memory_id, user_id=self.alice)

        self.assertEqual(self.store.stats(self.alice), {
            "entities": 1,
            "relations": 0,
        })
        self.assertEqual(self.store.stats(self.bob)["entities"], 1)


@unittest.skipUnless(
    RUN_REAL_STORES,
    "设置 HELLO_AGENTS_RUN_REAL_STORES=1 后才运行真实存储测试。",
)
class TestRealMemoryBackends(unittest.TestCase):
    @staticmethod
    def _qdrant_client():
        from qdrant_client import QdrantClient

        return QdrantClient(
            url=os.getenv(
                "HELLO_AGENTS_TEST_QDRANT_URL",
                "http://127.0.0.1:6333",
            )
        )

    @staticmethod
    def _neo4j_store() -> Neo4jGraphStore:
        return Neo4jGraphStore(
            uri=os.getenv(
                "HELLO_AGENTS_TEST_NEO4J_URI",
                "neo4j://127.0.0.1:7687",
            ),
            username=os.getenv("HELLO_AGENTS_TEST_NEO4J_USERNAME", "neo4j"),
            password=os.environ["HELLO_AGENTS_TEST_NEO4J_PASSWORD"],
        )

    def test_episodic_memory_real_qdrant_end_to_end(self):
        collection = f"hello_agents_episodic_it_{uuid4().hex}"
        client = self._qdrant_client()
        documents = SQLiteMemoryStore(":memory:")
        vectors = QdrantVectorStore(
            collection_name=collection,
            dimension=64,
            client=client,
        )
        memory = EpisodicMemory(
            document_store=documents,
            vector_store=vectors,
            embedder=HashingEmbedder(64),
            user_id="integration-user",
        )
        manager = MemoryManager(
            user_id="integration-user",
            backends={"episodic": memory},
        )
        try:
            memory_id = manager.add_memory(
                "昨天使用 Python 和 Qdrant 完成了向量检索测试",
                memory_type="episodic",
                importance=0.9,
            )

            results = manager.retrieve_memories(
                "Python Qdrant 向量检索",
                memory_types=["episodic"],
                limit=3,
            )

            self.assertEqual(results[0].item.id, memory_id)
            self.assertEqual(vectors.count(), 1)
            manager.remove_memory(memory_id, memory_type="episodic")
            self.assertEqual(vectors.count(), 0)
            self.assertEqual(len(memory), 0)
        finally:
            if client.collection_exists(collection):
                client.delete_collection(collection)
            client.close()
            documents.close()
    def test_semantic_memory_real_qdrant_and_neo4j_end_to_end(self):
        suffix = uuid4().hex
        user_id = f"semantic-{suffix}"
        collection = f"hello_agents_semantic_it_{suffix}"
        client = self._qdrant_client()
        documents = SQLiteMemoryStore(":memory:")
        vectors = QdrantVectorStore(
            collection_name=collection,
            dimension=64,
            client=client,
        )
        graph = self._neo4j_store()
        memory = SemanticMemory(
            document_store=documents,
            vector_store=vectors,
            graph_store=graph,
            embedder=HashingEmbedder(64),
            user_id=user_id,
        )
        item = MemoryItem(
            content="Python 用于构建 Agent 框架",
            memory_type="semantic",
            importance=0.9,
            metadata={
                "user_id": user_id,
                "entities": [
                    {"name": "Python", "type": "Technology"},
                    {"name": "Agent", "type": "Concept"},
                ],
                "relations": [
                    {
                        "source": "Python",
                        "target": "Agent",
                        "type": "用于构建",
                    }
                ],
            },
        )
        try:
            memory.add(item)

            results = memory.retrieve("用于构建", limit=3)

            self.assertEqual(results[0].item.id, item.id)
            self.assertEqual(vectors.count(), 1)
            self.assertEqual(graph.stats(user_id), {
                "entities": 2,
                "relations": 1,
            })
            memory.remove(item.id)
            self.assertEqual(vectors.count(), 0)
            self.assertEqual(graph.stats(user_id), {
                "entities": 0,
                "relations": 0,
            })
        finally:
            try:
                graph.clear_user(user_id)
            finally:
                graph.close()
            if client.collection_exists(collection):
                client.delete_collection(collection)
            client.close()
            documents.close()
