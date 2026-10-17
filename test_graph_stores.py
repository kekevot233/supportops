import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from hello_agents import (
    GraphEntity,
    GraphRelation,
    GraphStoreError,
    InMemoryGraphStore,
    Neo4jGraphStore,
)


def make_entity(
    entity_id="python",
    *,
    name="Python",
    user_id="alice",
    memory_id="memory-1",
):
    return GraphEntity(
        id=entity_id,
        name=name,
        entity_type="Technology",
        user_id=user_id,
        source_memory_ids=[memory_id],
        properties={"stable": True},
    )


def make_relation(
    relation_id="relation-1",
    *,
    source_id="python",
    target_id="agent",
    user_id="alice",
    memory_id="memory-1",
):
    return GraphRelation(
        id=relation_id,
        source_id=source_id,
        target_id=target_id,
        relation_type="用于构建",
        user_id=user_id,
        source_memory_id=memory_id,
    )


class FakeNeo4jDriver:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.closed = False

    def execute_query(self, query, *, parameters_, database_):
        self.calls.append(
            {
                "query": query,
                "parameters": parameters_,
                "database": database_,
            }
        )
        records = self.responses.pop(0) if self.responses else []
        return SimpleNamespace(records=records)

    def close(self):
        self.closed = True


class FailingNeo4jDriver(FakeNeo4jDriver):
    def execute_query(self, query, *, parameters_, database_):
        raise RuntimeError("secret neo4j detail")


class TestGraphModels(unittest.TestCase):
    def test_models_normalize_copy_and_validate(self):
        properties = {"nested": {"value": 1}}
        entity = GraphEntity(
            id=" entity ",
            name=" Python ",
            user_id=" alice ",
            source_memory_ids=[" one ", "one"],
            properties=properties,
        )
        properties["nested"]["value"] = 2

        self.assertEqual(entity.id, "entity")
        self.assertEqual(entity.source_memory_ids, ["one"])
        self.assertEqual(entity.properties["nested"]["value"], 1)

        with self.assertRaises(ValidationError):
            make_entity(memory_id=" ")
        with self.assertRaises(ValidationError):
            GraphRelation(
                id="r",
                source_id="a",
                target_id="b",
                relation_type=" ",
                user_id="alice",
                source_memory_id="m",
            )
        with self.assertRaises(ValidationError):
            make_entity().model_copy(
                update={"properties": {"bad": object()}},
            ).model_validate(
                make_entity().model_dump() | {"properties": {"bad": object()}}
            )


class TestInMemoryGraphStore(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryGraphStore()

    def test_upsert_search_relation_and_stats(self):
        python = make_entity()
        agent = make_entity("agent", name="Agent")
        self.store.upsert(
            [python, agent],
            [make_relation()],
        )

        results = self.store.search("Python 构建 Agent", user_id="alice", limit=5)

        self.assertEqual(results[0].memory_id, "memory-1")
        self.assertEqual(
            set(results[0].matched_entity_ids), {"python", "agent"}
        )
        self.assertEqual(
            self.store.stats("alice"), {"entities": 2, "relations": 1}
        )

    def test_shared_entity_survives_single_memory_deletion(self):
        self.store.upsert([make_entity(memory_id="memory-1")], [])
        self.store.upsert([make_entity(memory_id="memory-2")], [])

        self.assertEqual(
            {hit.memory_id for hit in self.store.search(
                "Python", user_id="alice", limit=5
            )},
            {"memory-1", "memory-2"},
        )
        self.store.delete_memory("memory-1", user_id="alice")

        self.assertEqual(
            [hit.memory_id for hit in self.store.search(
                "Python", user_id="alice", limit=5
            )],
            ["memory-2"],
        )
        self.assertEqual(self.store.stats("alice")["entities"], 1)

    def test_invalid_relation_is_atomic_and_user_scoped(self):
        with self.assertRaises(GraphStoreError):
            self.store.upsert(
                [make_entity()],
                [make_relation(target_id="missing")],
            )
        self.assertEqual(
            self.store.stats("alice"), {"entities": 0, "relations": 0}
        )

        self.store.upsert([make_entity()], [])
        with self.assertRaises(GraphStoreError):
            self.store.upsert(
                [make_entity("agent", name="Agent", user_id="bob")],
                [make_relation(target_id="agent")],
            )
        self.assertEqual(self.store.stats("bob")["entities"], 0)

    def test_clear_only_selected_user_and_validates_search(self):
        self.store.upsert([make_entity()], [])
        self.store.upsert(
            [make_entity("bob-python", user_id="bob", memory_id="bob-m")],
            [],
        )
        self.store.clear_user("alice")

        self.assertEqual(self.store.stats("alice")["entities"], 0)
        self.assertEqual(self.store.stats("bob")["entities"], 1)
        with self.assertRaises(GraphStoreError):
            self.store.search(" ", user_id="alice", limit=1)
        with self.assertRaises(GraphStoreError):
            self.store.search("Python", user_id="alice", limit=0)


class TestNeo4jGraphStore(unittest.TestCase):
    def setUp(self):
        self.driver = FakeNeo4jDriver()
        self.store = Neo4jGraphStore(driver=self.driver, database="memory")

    def test_upsert_uses_parameters_and_fixed_schema(self):
        dangerous_name = "Robert'); MATCH (n) DETACH DELETE n //"
        entity = make_entity(name=dangerous_name)
        self.store.upsert(
            [entity, make_entity("agent", name="Agent")],
            [make_relation()],
        )

        self.assertEqual(len(self.driver.calls), 2)
        entity_call, relation_call = self.driver.calls
        self.assertIn("MERGE (e:MemoryEntity", entity_call["query"])
        self.assertIn("reduce(", entity_call["query"])
        self.assertNotIn(dangerous_name, entity_call["query"])
        self.assertEqual(
            entity_call["parameters"]["rows"][0]["name"], dangerous_name
        )
        self.assertIn("[r:RELATED", relation_call["query"])
        self.assertEqual(entity_call["database"], "memory")

    def test_search_combines_entity_and_relation_hits(self):
        self.driver.responses = [
            [
                {
                    "memory_id": "memory-1",
                    "matched": 1,
                    "entity_ids": ["python"],
                }
            ],
            [
                {
                    "memory_id": "memory-1",
                    "matched": 1,
                    "entity_ids": ["python", "agent"],
                },
                {
                    "memory_id": "memory-2",
                    "matched": 1,
                    "entity_ids": ["neo4j"],
                },
            ],
        ]

        results = self.store.search("Python Agent", user_id="alice", limit=2)

        self.assertEqual([item.memory_id for item in results], [
            "memory-1", "memory-2"
        ])
        self.assertEqual(results[0].score, 1.0)
        self.assertEqual(
            set(results[0].matched_entity_ids), {"python", "agent"}
        )
        for call in self.driver.calls:
            self.assertEqual(call["parameters"]["user_id"], "alice")

    def test_delete_clear_stats_and_close(self):
        self.store.delete_memory("memory-1", user_id="alice")
        self.store.clear_user("alice")
        self.driver.responses = [[{"entities": 2, "relations": 1}]]
        self.assertEqual(
            self.store.stats("alice"), {"entities": 2, "relations": 1}
        )
        self.store.close()

        self.assertEqual(len(self.driver.calls), 4)
        self.assertTrue(self.driver.closed)

    def test_configuration_input_and_driver_failures_are_safe(self):
        with self.assertRaises(GraphStoreError):
            Neo4jGraphStore(uri="bolt://localhost", username="neo4j")
        with self.assertRaises(GraphStoreError):
            self.store.upsert([object()], [])

        failing = Neo4jGraphStore(driver=FailingNeo4jDriver())
        with self.assertRaises(GraphStoreError) as context:
            failing.search("Python", user_id="alice", limit=1)
        self.assertNotIn("secret", str(context.exception))
        self.assertIsInstance(context.exception.__cause__, RuntimeError)


if __name__ == "__main__":
    unittest.main()
