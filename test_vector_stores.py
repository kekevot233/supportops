import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from hello_agents import (
    InMemoryVectorStore,
    QdrantVectorStore,
    VectorRecord,
    VectorStoreError,
)


class FakeModels:
    class Distance:
        COSINE = "cosine"

    class VectorParams:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class PointStruct:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class MatchValue:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FieldCondition:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Filter:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class PointIdsList:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)


class FakeQdrantClient:
    def __init__(self):
        self.exists = False
        self.points = {}
        self.create_calls = []
        self.upsert_calls = []
        self.query_calls = []
        self.delete_calls = []
        self.delete_collection_calls = []

    def collection_exists(self, collection_name):
        return self.exists

    def create_collection(self, **kwargs):
        self.create_calls.append(kwargs)
        self.exists = True

    def upsert(self, **kwargs):
        self.upsert_calls.append(kwargs)
        for point in kwargs["points"]:
            self.points[str(point.id)] = point

    def query_points(self, **kwargs):
        self.query_calls.append(kwargs)
        points = [
            SimpleNamespace(
                id=point.id,
                score=0.75,
                payload=point.payload,
            )
            for point in self.points.values()
        ]
        return SimpleNamespace(points=points[: kwargs["limit"]])

    def delete(self, **kwargs):
        self.delete_calls.append(kwargs)
        for point_id in kwargs["points_selector"].points:
            self.points.pop(str(point_id), None)

    def delete_collection(self, collection_name):
        self.delete_collection_calls.append(collection_name)
        self.points.clear()
        self.exists = False

    def count(self, **kwargs):
        return SimpleNamespace(count=len(self.points))


class FailingQdrantClient(FakeQdrantClient):
    def query_points(self, **kwargs):
        raise RuntimeError("secret qdrant detail")


class TestVectorModels(unittest.TestCase):
    def test_record_owns_payload_and_validates_vector(self):
        payload = {"nested": {"value": 1}}
        record = VectorRecord(id=" point-1 ", vector=[1, 0], metadata=payload)
        payload["nested"]["value"] = 2

        self.assertEqual(record.id, "point-1")
        self.assertEqual(record.vector, [1.0, 0.0])
        self.assertEqual(record.metadata["nested"]["value"], 1)

        with self.assertRaises(ValidationError):
            VectorRecord(id="x", vector=[float("nan")])
        with self.assertRaises(ValidationError):
            VectorRecord(id="x", vector=[True])
        with self.assertRaises(ValidationError):
            VectorRecord(id="x", vector=[1], metadata={"bad": object()})


class TestInMemoryVectorStore(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryVectorStore(3)

    def test_upsert_search_filter_and_replace(self):
        first = VectorRecord(
            id="first",
            vector=[1, 0, 0],
            metadata={"user_id": "alice", "kind": "note"},
        )
        second = VectorRecord(
            id="second",
            vector=[0, 1, 0],
            metadata={"user_id": "bob", "kind": "note"},
        )
        self.assertEqual(self.store.upsert([first, second]), 2)

        results = self.store.search(
            [1, 0, 0],
            limit=5,
            filters={"user_id": "alice"},
        )
        self.assertEqual([result.id for result in results], ["first"])
        self.assertAlmostEqual(results[0].score, 1.0)

        replacement = VectorRecord(
            id="first",
            vector=[0, 0, 1],
            metadata={"user_id": "alice", "kind": "updated"},
        )
        self.store.upsert([replacement])
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(
            self.store.search([0, 0, 1], limit=1)[0].metadata["kind"],
            "updated",
        )

    def test_results_are_isolated_copies(self):
        self.store.upsert(
            [VectorRecord(id="x", vector=[1, 0, 0], metadata={"value": 1})]
        )
        result = self.store.search([1, 0, 0], limit=1)[0]
        result.metadata["value"] = 99

        fresh = self.store.search([1, 0, 0], limit=1)[0]
        self.assertEqual(fresh.metadata["value"], 1)

    def test_delete_clear_and_count(self):
        self.store.upsert(
            [
                VectorRecord(id="a", vector=[1, 0, 0]),
                VectorRecord(id="b", vector=[0, 1, 0]),
            ]
        )
        self.store.delete(["a", "missing"])
        self.assertEqual(self.store.count(), 1)
        self.store.clear()
        self.assertEqual(self.store.count(), 0)

    def test_rejects_invalid_dimensions_batches_queries_and_ids(self):
        with self.assertRaises(VectorStoreError):
            InMemoryVectorStore(0)
        with self.assertRaises(VectorStoreError):
            self.store.upsert([])
        with self.assertRaises(VectorStoreError):
            self.store.upsert(
                [VectorRecord(id="x", vector=[1, 0])]
            )
        with self.assertRaises(VectorStoreError):
            self.store.upsert(
                [
                    VectorRecord(id="x", vector=[1, 0, 0]),
                    VectorRecord(id="x", vector=[0, 1, 0]),
                ]
            )
        with self.assertRaises(VectorStoreError):
            self.store.search([1, 0], limit=1)
        with self.assertRaises(VectorStoreError):
            self.store.search([1, 0, 0], limit=0)
        with self.assertRaises(VectorStoreError):
            self.store.delete([])


class TestQdrantVectorStore(unittest.TestCase):
    def setUp(self):
        self.client = FakeQdrantClient()
        self.store = QdrantVectorStore(
            collection_name="episodic_memory",
            dimension=3,
            client=self.client,
            models_module=FakeModels,
        )

    def test_creates_collection_upserts_and_counts(self):
        count = self.store.upsert(
            [
                VectorRecord(
                    id="point-1",
                    vector=[1, 0, 0],
                    metadata={"user_id": "alice"},
                )
            ]
        )

        self.assertEqual(count, 1)
        self.assertEqual(len(self.client.create_calls), 1)
        vector_config = self.client.create_calls[0]["vectors_config"]
        self.assertEqual(vector_config.size, 3)
        self.assertEqual(vector_config.distance, "cosine")
        self.assertEqual(self.store.count(), 1)
        stored_point = next(iter(self.client.points.values()))
        self.assertNotEqual(stored_point.id, "point-1")
        self.assertEqual(
            stored_point.payload["_hello_agents_record_id"],
            "point-1",
        )

    def test_search_builds_exact_payload_filter(self):
        self.store.upsert(
            [
                VectorRecord(
                    id="point-1",
                    vector=[1, 0, 0],
                    metadata={"user_id": "alice", "kind": "episodic"},
                )
            ]
        )

        results = self.store.search(
            [1, 0, 0],
            limit=2,
            filters={"user_id": "alice", "kind": "episodic"},
        )

        self.assertEqual(results[0].id, "point-1")
        self.assertNotIn("_hello_agents_record_id", results[0].metadata)
        query_filter = self.client.query_calls[0]["query_filter"]
        conditions = {
            condition.key: condition.match.value
            for condition in query_filter.must
        }
        self.assertEqual(
            conditions,
            {"user_id": "alice", "kind": "episodic"},
        )

    def test_delete_and_clear(self):
        self.store.upsert(
            [VectorRecord(id="point-1", vector=[1, 0, 0])]
        )
        self.store.delete(["point-1"])
        self.assertEqual(self.store.count(), 0)

        self.store.upsert(
            [VectorRecord(id="point-2", vector=[0, 1, 0])]
        )
        self.store.clear()
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(
            self.client.delete_collection_calls,
            ["episodic_memory"],
        )

    def test_rejects_invalid_configuration_and_dimensions(self):
        with self.assertRaises(VectorStoreError):
            QdrantVectorStore(collection_name="bad name", dimension=3, url="x")
        with self.assertRaises(VectorStoreError):
            QdrantVectorStore(collection_name="valid", dimension=3)
        with self.assertRaises(VectorStoreError):
            self.store.upsert(
                [VectorRecord(id="x", vector=[1, 0])]
            )

    def test_client_failures_are_wrapped_without_detail_leakage(self):
        store = QdrantVectorStore(
            collection_name="episodic_memory",
            dimension=3,
            client=FailingQdrantClient(),
            models_module=FakeModels,
        )

        with self.assertRaises(VectorStoreError) as context:
            store.search([1, 0, 0], limit=1)

        self.assertNotIn("secret", str(context.exception))
        self.assertIsInstance(context.exception.__cause__, RuntimeError)


if __name__ == "__main__":
    unittest.main()
