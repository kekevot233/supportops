"""P2：有界字节接入、发布隔离、真实本地检索及 Fake 故障，无外部服务。"""

import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

from hello_agents import DocumentRAGResult
from supportops import ApplicationDatabase, DemoIdentityProvider, DocumentService, LocalDocumentIndex, SupportOpsError, WorkspaceService
from supportops.__main__ import main
from supportops.database import APPLICATION_ID, SCHEMA_VERSION, _SCHEMA, _P2_SCHEMA
from supportops.originals import SQLiteOriginalStore


class DocumentFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "app.sqlite3"
        self.database = ApplicationDatabase(self.path)
        self.addCleanup(self.database.close)
        self.database.bootstrap_demo(enabled=True)
        self.provider = DemoIdentityProvider(enabled=True)
        self.workspace = WorkspaceService(self.database, self.provider)
        self.now = 1000.0
        self.index = LocalDocumentIndex()
        self.service = DocumentService(self.database, self.provider, index=self.index, clock=lambda: self.now)

    def upload(self, text="# 部署\n\n服务端口为 8080。", key="upload-1", *, workspace="helloagent", doc=None, title="部署手册"):
        return self.service.upload("admin", workspace, {"filename": "guide.md", "title": title,
            "idempotency_key": key}, text.encode("utf-8"), document_id=doc)

    def publish(self, **kwargs):
        job = self.upload(**kwargs)
        self.assertEqual(self.service.process_next("admin", kwargs.get("workspace", "helloagent")).state, "succeeded")
        return job

    def assert_error(self, code, action):
        with self.assertRaises(SupportOpsError) as context:
            action()
        self.assertEqual(context.exception.code, code)
        return context.exception

    def count(self, table):
        with self.database.transaction() as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


class TestDocumentIngestion(DocumentFixture):
    def test_pending_version_not_searchable_until_worker_publishes(self):
        job = self.upload()
        self.assertEqual(job.state, "pending")
        self.assertIsNone(self.service.get_document("reader", "helloagent", job.document_id).current_version_id)
        self.assertEqual(self.service.search("reader", "helloagent", "端口").hits, [])
        completed = self.service.process_next("admin", "helloagent")
        self.assertEqual((completed.state, completed.attempts), ("succeeded", 1))
        result = self.service.search("reader", "helloagent", "服务端口")
        self.assertIn("8080", result.hits[0].chunk.content)
        self.assertEqual(result.hits[0].chunk.metadata["version_id"], job.version_id)

    def test_upload_is_idempotent_but_key_cannot_change_payload(self):
        first = self.upload()
        self.assertEqual(first, self.upload())
        for kwargs in ({"text": "另一段正文"}, {"title": "别的标题"}):
            self.assert_error("CONFLICT", lambda: self.upload(**kwargs))
        self.assertEqual(self.count("documents"), 1)
        self.assertEqual(self.count("document_versions"), 1)
        self.assertEqual(self.count("ingestion_jobs"), 1)

    def test_same_key_in_different_spaces_creates_independent_documents(self):
        first = self.upload()
        other = self.upload(workspace="internal-lab")
        self.assertNotEqual(first.document_id, other.document_id)
        self.assertEqual(self.count("ingestion_jobs"), 2)

    def test_rejects_filename_paths_hidden_files_binary_and_unsupported_formats(self):
        for filename in ("../guide.md", "C:\\guide.md", ".env", ".env.md", "guide.pdf", "folder/guide.txt"):
            with self.subTest(filename=filename):
                self.assert_error("INVALID_REQUEST", lambda: self.service.upload("admin", "helloagent", {
                    "filename": filename, "title": "guide", "idempotency_key": "bad"}, b"data"))
        for content in (b"", b"   ", b"\xff", b"a\x00b", "not bytes"):
            self.assert_error("INVALID_REQUEST", lambda: self.service.upload("admin", "helloagent", {
                "filename": "guide.md", "title": "guide", "idempotency_key": "bad"}, content))
        self.assertEqual(self.count("documents"), 0)

    def test_oversize_and_spoofed_actor_fields_rejected_without_writes(self):
        payload = {"filename": "guide.md", "title": "guide", "idempotency_key": "bad"}
        self.assert_error("UPLOAD_TOO_LARGE", lambda: self.service.upload("admin", "helloagent", payload, b"a" * 256001))
        self.assert_error("INVALID_REQUEST", lambda: self.service.upload("admin", "helloagent", {**payload, "user_id": "admin"}, b"data"))
        self.assertEqual(self.count("document_originals"), 0)

    def test_original_bytes_preserved_and_citation_offsets_use_normalized_text(self):
        raw = "\ufeff# 部署\r\n\r\n端口 8080。\r\n".encode("utf-8")
        job = self.service.upload("admin", "helloagent", {"filename": "guide.txt", "title": "guide", "idempotency_key": "bom"}, raw)
        with self.database.transaction() as connection:
            self.assertEqual(SQLiteOriginalStore().get(connection, "helloagent", job.document_id, job.version_id)["content"], raw)
        self.assertEqual(self.service.process_next("admin", "helloagent").state, "succeeded")
        result = self.service.search("reader", "helloagent", "端口")
        self.assertTrue(result.hits)
        for hit in result.hits:
            self.assertEqual("# 部署\n\n端口 8080。"[hit.chunk.start_char:hit.chunk.end_char], hit.chunk.content)

    def test_original_write_or_audit_failure_rolls_back_entire_upload(self):
        for target in ("_originals.put", "_database.record_audit"):
            owner, attribute = target.split(".")
            with patch.object(getattr(self.service, owner), attribute, side_effect=sqlite3.OperationalError("private path")):
                self.assert_error("STORAGE_UNAVAILABLE", self.upload)
            self.assertEqual(self.count("documents"), 0)
            self.assertEqual(self.count("document_versions"), 0)
            self.assertEqual(self.count("document_originals"), 0)
            self.assertEqual(self.count("ingestion_jobs"), 0)

    def test_capacity_limit_does_not_leave_partial_document(self):
        with patch("supportops.documents.MAX_DOCUMENTS", 1):
            self.upload()
            self.assert_error("CAPACITY_EXCEEDED", lambda: self.upload(key="second"))
        with patch("supportops.documents.MAX_VERSIONS", 1):
            job = self.service.list_jobs("reader", "helloagent")[0]
            self.assert_error("CAPACITY_EXCEEDED", lambda: self.upload(key="v2", doc=job.document_id))
        self.assertEqual(self.count("documents"), 1)


class TestDocumentVersionsAndDeletion(DocumentFixture):
    def test_failed_update_keeps_old_version_and_success_changes_title_and_content(self):
        first = self.publish()
        update = self.upload(text="# 部署\n\n服务端口改为 9090。", key="v2", doc=first.document_id, title="新版手册")
        with patch.object(self.index, "build", side_effect=RuntimeError("secret upstream response")):
            failed = self.service.process_next("admin", "helloagent")
        self.assertEqual((failed.state, failed.error_code), ("failed", "INDEX_BUILD_FAILED"))
        doc = self.service.get_document("reader", "helloagent", first.document_id)
        self.assertEqual((doc.current_version_id, doc.title), (first.version_id, "部署手册"))
        self.assertIn("8080", self.service.search("reader", "helloagent", "端口").hits[0].chunk.content)
        self.service.retry_job("admin", "helloagent", update.id)
        self.assertEqual(self.service.process_next("admin", "helloagent").state, "succeeded")
        doc = self.service.get_document("reader", "helloagent", first.document_id)
        self.assertEqual((doc.current_version_id, doc.title), (update.version_id, "新版手册"))
        self.assertTrue(all("8080" not in hit.chunk.content for hit in self.service.search("reader", "helloagent", "端口").hits))

    def test_historical_citation_keeps_its_version_not_new_text(self):
        first = self.publish()
        hit = self.service.search("reader", "helloagent", "端口").hits[0]
        self.publish(text="新版端口为 9090。", key="v2", doc=first.document_id)
        source = self.service.read_citation("reader", "helloagent", first.document_id, first.version_id, hit.chunk.id)
        self.assertFalse(source["is_current"])
        self.assertIn("8080", source["chunk"]["content"])

    def test_delete_removes_originals_all_version_indexes_and_invalidates_citations(self):
        first = self.publish()
        hit = self.service.search("reader", "helloagent", "端口").hits[0]
        self.publish(text="新版端口 9090。", key="v2", doc=first.document_id)
        self.upload(text="待处理新版。", key="v3", doc=first.document_id)
        deleted = self.service.delete_document("admin", "helloagent", first.document_id)
        self.assertEqual(deleted, self.service.delete_document("admin", "helloagent", first.document_id))
        self.assertEqual(self.count("document_originals"), 0)
        self.assertEqual(self.count("indexed_chunks"), 0)
        self.assertEqual(self.service.list_documents("reader", "helloagent"), [])
        self.assertEqual(self.service.search("reader", "helloagent", "端口").hits, [])
        self.assert_error("SOURCE_GONE", lambda: self.service.read_citation("reader", "helloagent", first.document_id, first.version_id, hit.chunk.id))
        self.assertIsNone(self.service.process_next("admin", "helloagent"))
        self.assert_error("SOURCE_GONE", lambda: self.upload())

    def test_delete_failure_does_not_partially_hide_document(self):
        job = self.publish()
        with patch.object(self.service._originals, "delete", side_effect=sqlite3.OperationalError("fake cleanup failure")):
            self.assert_error("STORAGE_UNAVAILABLE", lambda: self.service.delete_document("admin", "helloagent", job.document_id))
        self.assertEqual(self.service.get_document("reader", "helloagent", job.document_id).current_version_id, job.version_id)
        self.assertGreater(self.count("indexed_chunks"), 0)

    def test_old_job_finishing_after_newer_job_cannot_rollback_published_version(self):
        old = self.upload()
        newer = self.upload(text="新版端口 9090。", key="v2", doc=old.document_id)
        build = self.index.build
        def finish_newer(document):
            with patch.object(self.index, "build", side_effect=build):
                self.assertEqual(self.service.process_next("admin", "helloagent").id, newer.id)
            return build(document)
        with patch.object(self.index, "build", side_effect=finish_newer):
            result = self.service.process_next("admin", "helloagent")
        self.assertEqual((result.state, result.error_code), ("cancelled", "SUPERSEDED"))
        self.assertEqual(self.service.get_document("reader", "helloagent", old.document_id).current_version_id, newer.version_id)

    def test_delete_during_build_fences_old_worker(self):
        job = self.upload()
        build = self.index.build
        def delete(document):
            entries = build(document)
            self.service.delete_document("admin", "helloagent", job.document_id)
            return entries
        with patch.object(self.index, "build", side_effect=delete):
            self.assert_error("CONFLICT", lambda: self.service.process_next("admin", "helloagent"))
        self.assertEqual(self.count("indexed_chunks"), 0)
        self.assertEqual(self.service.get_job("reader", "helloagent", job.id).state, "cancelled")


class TestDocumentQueueRecovery(DocumentFixture):
    def test_failed_older_version_cannot_be_retried_after_newer_publication(self):
        first = self.upload()
        with patch.object(self.index, "build", side_effect=RuntimeError("fixture")):
            self.service.process_next("admin", "helloagent")
        self.publish(text="新版内容。", key="v2", doc=first.document_id)
        self.assert_error("CONFLICT", lambda: self.service.retry_job("admin", "helloagent", first.id))

    def test_repeated_expired_crashes_stop_at_attempt_limit(self):
        job = self.upload()
        for _ in range(3):
            with patch.object(self.index, "build", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.service.process_next("admin", "helloagent")
            self.now += 61
        self.assertIsNone(self.service.process_next("admin", "helloagent"))
        result = self.service.get_job("reader", "helloagent", job.id)
        self.assertEqual((result.state, result.attempts, result.error_code), ("failed", 3, "LEASE_EXPIRED"))
        self.assert_error("CONFLICT", lambda: self.service.retry_job("admin", "helloagent", job.id))

    def test_pending_task_and_published_index_survive_reopen(self):
        job = self.upload()
        self.database.close()
        with ApplicationDatabase(self.path) as reopened:
            service = DocumentService(reopened, self.provider)
            self.assertEqual(service.get_job("reader", "helloagent", job.id).state, "pending")
            self.assertEqual(service.process_next("admin", "helloagent").state, "succeeded")
        with ApplicationDatabase(self.path) as reopened:
            result = DocumentService(reopened, self.provider).search("reader", "helloagent", "端口")
            self.assertIn("8080", result.hits[0].chunk.content)

    def test_crashed_running_task_waits_for_lease_then_is_reclaimed(self):
        job = self.upload()
        with patch.object(self.index, "build", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.service.process_next("admin", "helloagent")
        self.assertEqual(self.service.get_job("reader", "helloagent", job.id).state, "running")
        self.assertIsNone(self.service.process_next("admin", "helloagent"))
        self.now += 61
        with ApplicationDatabase(self.path) as reopened:
            service = DocumentService(reopened, self.provider, clock=lambda: self.now)
            result = service.process_next("admin", "helloagent")
            self.assertEqual((result.state, result.attempts), ("succeeded", 2))

    def test_stale_lease_token_cannot_overwrite_new_worker(self):
        job = self.upload()
        build = self.index.build
        def finish_after_reclaim(document):
            self.now += 61
            with patch.object(self.index, "build", side_effect=build):
                self.assertEqual(self.service.process_next("admin", "helloagent").state, "succeeded")
            return build(document)
        with patch.object(self.index, "build", side_effect=finish_after_reclaim):
            self.assert_error("CONFLICT", lambda: self.service.process_next("admin", "helloagent"))
        self.assertEqual(self.service.get_job("reader", "helloagent", job.id).attempts, 2)
        self.assertGreater(self.count("indexed_chunks"), 0)

    def test_retry_attempt_limit_and_nonfailed_task_rejection(self):
        job = self.upload()
        self.assert_error("CONFLICT", lambda: self.service.retry_job("admin", "helloagent", job.id))
        with patch.object(self.index, "build", side_effect=RuntimeError("fixture failure")):
            for attempt in range(1, 4):
                result = self.service.process_next("admin", "helloagent")
                self.assertEqual((result.state, result.attempts), ("failed", attempt))
                if attempt < 3:
                    self.service.retry_job("admin", "helloagent", job.id)
        self.assert_error("CONFLICT", lambda: self.service.retry_job("admin", "helloagent", job.id))

    def test_publish_audit_failure_rolls_back_index_and_can_recover(self):
        job = self.upload()
        with patch.object(self.database, "record_audit", side_effect=sqlite3.OperationalError("fake audit failure")):
            self.assert_error("STORAGE_UNAVAILABLE", lambda: self.service.process_next("admin", "helloagent"))
        self.assertEqual(self.count("indexed_chunks"), 0)
        self.assertIsNone(self.service.get_document("reader", "helloagent", job.document_id).current_version_id)
        self.now += 61
        self.assertEqual(self.service.process_next("admin", "helloagent").state, "succeeded")

    def test_corrupt_original_fails_with_safe_code(self):
        self.upload()
        with self.database.transaction(write=True) as connection:
            connection.execute("UPDATE document_originals SET content=?", (b"tampered",))
        result = self.service.process_next("admin", "helloagent")
        self.assertEqual((result.state, result.error_code), ("failed", "ORIGINAL_UNAVAILABLE"))

    def test_invalid_index_evidence_not_published(self):
        self.upload()
        build = self.index.build
        def corrupt(document):
            entries = build(document)
            entries[0].chunk = entries[0].chunk.model_copy(update={"content": "not original"})
            return entries
        with patch.object(self.index, "build", side_effect=corrupt):
            self.assertEqual(self.service.process_next("admin", "helloagent").state, "failed")
        self.assertEqual(self.count("indexed_chunks"), 0)


class TestDocumentAccessAndSearch(DocumentFixture):
    def test_mismatched_citations_and_corrupt_persisted_scope_fail_closed(self):
        self.publish()
        valid = self.service.search("reader", "helloagent", "端口")
        with patch.object(self.index, "query", return_value=valid.model_copy(update={"citations": []})):
            self.assert_error("INDEX_UNAVAILABLE", lambda: self.service.search("reader", "helloagent", "端口"))
        with self.database.transaction(write=True) as connection:
            row = connection.execute("SELECT data_json FROM indexed_chunks LIMIT 1").fetchone()
            data = json.loads(row["data_json"])
            data["chunk"]["metadata"]["workspace_id"] = "internal-lab"
            connection.execute("UPDATE indexed_chunks SET data_json=?", (json.dumps(data),))
        self.assert_error("INDEX_UNAVAILABLE", lambda: self.service.search("reader", "helloagent", "端口"))
        chunk = valid.hits[0].chunk
        self.assert_error("INDEX_UNAVAILABLE", lambda: self.service.read_citation(
            "reader", "helloagent", chunk.document_id, chunk.metadata["version_id"], chunk.id))

    def test_two_spaces_can_search_only_their_own_published_text(self):
        self.publish(text="公开演示端口 8080。")
        private = self.publish(text="隔离演示端口 9090。", workspace="internal-lab")
        result = self.service.search("reader", "helloagent", "端口")
        self.assertTrue(result.hits)
        self.assertTrue(all("9090" not in hit.chunk.content for hit in result.hits))
        other = self.service.search("admin", "internal-lab", "端口")
        chunk_id = other.hits[0].chunk.id
        self.assert_error("NOT_FOUND", lambda: self.service.read_citation(
            "reader", "helloagent", private.document_id, private.version_id, chunk_id))

    def test_every_resource_entrypoint_rejects_hidden_space(self):
        job = self.publish(workspace="internal-lab")
        operations = [lambda: self.service.list_documents("reader", "internal-lab"),
            lambda: self.service.get_document("reader", "internal-lab", job.document_id),
            lambda: self.service.list_jobs("reader", "internal-lab"),
            lambda: self.service.get_job("reader", "internal-lab", job.id),
            lambda: self.service.search("reader", "internal-lab", "端口"),
            lambda: self.service.read_citation("reader", "internal-lab", job.document_id, job.version_id, "chunk"),
            lambda: self.service.retry_job("reader", "internal-lab", job.id),
            lambda: self.service.delete_document("reader", "internal-lab", job.document_id),
            lambda: self.service.process_next("reader", "internal-lab"),
            lambda: self.service.upload("reader", "internal-lab", {}, b"data")]
        for operation in operations:
            self.assert_error("NOT_FOUND", operation)

    def test_reader_cannot_upload_retry_delete_or_run_worker(self):
        job = self.publish()
        operations = [lambda: self.service.upload("reader", "helloagent", {}, b"data"),
            lambda: self.service.retry_job("reader", "helloagent", job.id),
            lambda: self.service.delete_document("reader", "helloagent", job.document_id),
            lambda: self.service.process_next("reader", "helloagent")]
        for operation in operations:
            self.assert_error("FORBIDDEN", operation)

    def test_editor_can_ingest_but_cannot_manage_members(self):
        self.workspace.set_member("admin", "helloagent", {"member_id": "demo-reader", "role": "editor"})
        job = self.service.upload("reader", "helloagent", {"filename": "g.md", "title": "g", "idempotency_key": "reader"}, b"hello")
        self.assertEqual(self.service.process_next("reader", "helloagent").id, job.id)
        self.assert_error("FORBIDDEN", lambda: self.workspace.list_members("reader", "helloagent"))

    def test_wrong_space_ids_cannot_fetch_other_document_job_or_citation(self):
        private = self.publish(workspace="internal-lab")
        self.assert_error("NOT_FOUND", lambda: self.service.get_document("reader", "helloagent", private.document_id))
        self.assert_error("NOT_FOUND", lambda: self.service.get_job("reader", "helloagent", private.id))
        self.assertEqual(self.service.search("reader", "helloagent", "端口").hits, [])

    def test_revoked_identity_cannot_publish_or_read_old_citation(self):
        self.workspace.set_member("admin", "helloagent", {"member_id": "demo-reader", "role": "editor"})
        self.upload()
        build = self.index.build
        def revoke(document):
            entries = build(document)
            self.workspace.remove_member("admin", "helloagent", "demo-reader")
            return entries
        with patch.object(self.index, "build", side_effect=revoke):
            self.assert_error("NOT_FOUND", lambda: self.service.process_next("reader", "helloagent"))
        self.assertEqual(self.count("indexed_chunks"), 0)
        self.assert_error("NOT_FOUND", lambda: self.service.search("reader", "helloagent", "端口"))

    def test_backend_failure_or_profile_mismatch_not_reported_as_no_evidence(self):
        self.publish()
        with patch.object(self.index, "query", side_effect=RuntimeError("private details")):
            error = self.assert_error("INDEX_UNAVAILABLE", lambda: self.service.search("reader", "helloagent", "端口"))
            self.assertNotIn("private", str(error))
        with patch.object(self.index, "profile", "different-model"):
            self.assert_error("INDEX_UNAVAILABLE", lambda: self.service.search("reader", "helloagent", "端口"))

    def test_adapter_cannot_return_unscoped_evidence(self):
        self.publish(workspace="internal-lab")
        private = self.service.search("admin", "internal-lab", "端口")
        with patch.object(self.index, "query", return_value=private):
            self.assert_error("INDEX_UNAVAILABLE", lambda: self.service.search("reader", "helloagent", "端口"))

    def test_invalid_search_arguments_are_rejected(self):
        for question in (None, "", " ", "x" * 2001):
            self.assert_error("INVALID_REQUEST", lambda: self.service.search("reader", "helloagent", question))
        for limit in (True, 0, 21):
            self.assert_error("INVALID_REQUEST", lambda: self.service.search("reader", "helloagent", "端口", limit=limit))


class TestDocumentMigration(unittest.TestCase):
    def test_v1_migration_preserves_users_and_memberships(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v1.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute("PRAGMA user_version=1")
                connection.execute("INSERT INTO users VALUES('existing','existing',1,'now')")
                connection.execute("INSERT INTO workspaces VALUES('existing','workspace','existing','now')")
                connection.execute("INSERT INTO memberships VALUES('existing','existing','admin','now')")
                connection.commit()
            with ApplicationDatabase(path) as database, database.transaction() as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(connection.execute("SELECT user_id FROM memberships").fetchone()[0], "existing")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_failed_migration_rolls_back_ddl_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "v1.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute("PRAGMA user_version=1")
            with patch("supportops.database._P2_SCHEMA", (_P2_SCHEMA[0], "INVALID SQL")):
                with self.assertRaises(SupportOpsError):
                    ApplicationDatabase(path)
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='document_originals'").fetchone())


class TestDocumentConcurrency(DocumentFixture):
    def test_concurrent_uploads_share_one_idempotent_job(self):
        barrier = Barrier(2)
        def upload():
            with ApplicationDatabase(self.path) as database:
                service = DocumentService(database, self.provider)
                barrier.wait(timeout=5)
                return service.upload("admin", "helloagent", {
                    "filename": "g.md", "title": "guide", "idempotency_key": "concurrent"}, b"hello")
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(upload), pool.submit(upload)
            self.assertEqual(first.result(), second.result())
        self.assertEqual(self.count("ingestion_jobs"), 1)
        self.assertEqual(self.count("document_originals"), 1)

    def test_second_worker_cannot_claim_an_unexpired_running_job(self):
        job = self.upload()
        entered, release = Event(), Event()
        build = self.index.build
        def paused(document):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test timed out")
            return build(document)
        with ApplicationDatabase(self.path) as second_database:
            second = DocumentService(second_database, self.provider, clock=lambda: self.now)
            with ThreadPoolExecutor(max_workers=1) as pool, patch.object(self.index, "build", side_effect=paused):
                future = pool.submit(self.service.process_next, "admin", "helloagent")
                try:
                    self.assertTrue(entered.wait(timeout=5))
                    self.assertIsNone(second.process_next("admin", "helloagent"))
                finally:
                    release.set()
                self.assertEqual(future.result().state, "succeeded")
        self.assertEqual(self.service.get_job("reader", "helloagent", job.id).attempts, 1)


class TestDocumentCLI(unittest.TestCase):
    def test_upload_work_search_and_confirmed_delete_in_separate_cli_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = ["demo", "--enable-demo", "--profile", "admin", "--database", str(root / "outputs" / "app.sqlite3")]
            def invoke(*args):
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(io.StringIO()):
                    code = main([*command, *args])
                return code, json.loads(output.getvalue()) if output.getvalue() else None
            with patch("supportops.__main__.__file__", str(root / "supportops" / "__main__.py")):
                self.assertEqual(invoke("init")[0], 0)
                code, payload = invoke("upload", "helloagent", "--filename", "guide.md", "--title", "guide",
                                       "--content", "服务端口为 8080。", "--key", "v1")
                self.assertEqual(code, 0)
                doc = payload["result"]["document_id"]
                self.assertEqual(invoke("work", "helloagent")[1]["result"]["job"]["state"], "succeeded")
                self.assertEqual(invoke("search", "helloagent", "端口")[1]["result"]["mode"], "retrieval_only")
                self.assertEqual(invoke("delete", "helloagent", doc)[0], 2)
                self.assertEqual(invoke("delete", "helloagent", doc, "--confirm")[0], 0)
                self.assertEqual(invoke("search", "helloagent", "端口")[1]["result"]["hits"], [])


if __name__ == "__main__":
    unittest.main()
