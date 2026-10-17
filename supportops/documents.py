"""文档接入、持久化队列与版本发布；所有公开资源入口均先校验空间权限。"""

from __future__ import annotations

import hashlib
import json
import math
import time
from contextlib import contextmanager

from hello_agents import Citation, Document, DocumentRAGResult, VectorRecord

from .database import ApplicationDatabase, new_id, utc_now
from .document_index import DocumentIndex, IndexEntry, LocalDocumentIndex
from .document_models import DocumentView, JobView, UploadRequest
from .errors import SupportOpsError
from .identity import IdentityProvider
from .originals import MAX_UPLOAD_BYTES, OriginalStore, SQLiteOriginalStore, decode_document
from .workspaces import WorkspaceService

MAX_ATTEMPTS = 3
MAX_DOCUMENTS = 100
MAX_VERSIONS = 20
MAX_SEARCH_CHUNKS = 2000


class DocumentService:
    def __init__(self, database: ApplicationDatabase, identity_provider: IdentityProvider, *,
                 index: DocumentIndex | None = None, originals: OriginalStore | None = None,
                 clock=time.time, lease_seconds: float = 60):
        if (isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(lease_seconds) or not 1 <= lease_seconds <= 3600):
            raise SupportOpsError("INVALID_REQUEST")
        self._database = database
        self._access = WorkspaceService(database, identity_provider)
        self._index = index if index is not None else LocalDocumentIndex()
        self._originals = originals if originals is not None else SQLiteOriginalStore()
        self._clock, self._lease_seconds = clock, lease_seconds

    @contextmanager
    def _scope(self, credential, workspace_id, *, write=False):
        identity = self._access._identity(credential)
        with self._database.transaction(write=write) as connection:
            self._access._authorize(connection, identity, workspace_id, write=write)
            yield connection, identity

    @staticmethod
    def _document(connection, workspace_id, document_id):
        if not isinstance(document_id, str):
            raise SupportOpsError("NOT_FOUND")
        row = connection.execute("SELECT * FROM documents WHERE workspace_id=? AND id=?",
                                 (workspace_id, document_id)).fetchone()
        if row is None:
            raise SupportOpsError("NOT_FOUND")
        if connection.execute("SELECT 1 FROM document_tombstones WHERE workspace_id=? AND document_id=?",
                              (workspace_id, document_id)).fetchone():
            raise SupportOpsError("SOURCE_GONE")
        return row

    @staticmethod
    def _job(connection, workspace_id, job_id):
        if not isinstance(job_id, str):
            raise SupportOpsError("NOT_FOUND")
        row = connection.execute("SELECT * FROM ingestion_jobs WHERE workspace_id=? AND id=?",
                                 (workspace_id, job_id)).fetchone()
        if row is None:
            raise SupportOpsError("NOT_FOUND")
        return row

    @staticmethod
    def _job_view(row):
        return JobView(**{field: row[field] for field in JobView.model_fields})

    def _view(self, connection, workspace_id, document_id):
        doc = self._document(connection, workspace_id, document_id)
        latest = connection.execute(
            "SELECT id,version_number,state FROM document_versions WHERE workspace_id=? AND document_id=? "
            "ORDER BY version_number DESC LIMIT 1", (workspace_id, document_id)).fetchone()
        if latest is None:
            raise SupportOpsError("STORAGE_UNAVAILABLE")
        return DocumentView(id=doc["id"], workspace_id=workspace_id, title=doc["title"],
                            current_version_id=doc["current_version_id"], latest_version_id=latest["id"],
                            latest_version_number=latest["version_number"], latest_state=latest["state"])

    def upload(self, credential, workspace_id, payload: dict, content: bytes, *, document_id=None) -> JobView:
        """接收传入字节，不打开 filename；相同空间内的幂等键绑定完整请求摘要。"""
        with self._scope(credential, workspace_id, write=True) as (connection, identity):
            request = self._access._parse(UploadRequest, payload)
            if not isinstance(content, bytes):
                raise SupportOpsError("INVALID_REQUEST")
            if len(content) > MAX_UPLOAD_BYTES:
                raise SupportOpsError("UPLOAD_TOO_LARGE")
            try:
                decode_document(content)
            except (UnicodeError, ValueError):
                raise SupportOpsError("INVALID_REQUEST") from None
            digest = hashlib.sha256(content).hexdigest()
            if document_id is not None:
                self._document(connection, workspace_id, document_id)
            fingerprint = hashlib.sha256(json.dumps({"document_id": document_id, "sha256": digest,
                "title": request.title, "filename": request.filename}, sort_keys=True).encode()).hexdigest()
            old = connection.execute("SELECT * FROM ingestion_jobs WHERE workspace_id=? AND idempotency_key=?",
                                     (workspace_id, request.idempotency_key)).fetchone()
            if old:
                self._document(connection, workspace_id, old["document_id"])
                if old["request_sha256"] != fingerprint:
                    raise SupportOpsError("CONFLICT")
                return self._job_view(old)
            if document_id is None:
                count = connection.execute("SELECT count(*) FROM documents WHERE workspace_id=?", (workspace_id,)).fetchone()[0]
                if count >= MAX_DOCUMENTS:
                    raise SupportOpsError("CAPACITY_EXCEEDED")
                document_id = new_id("doc")
                connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?)",
                                   (document_id, workspace_id, request.title, None, identity.subject, utc_now()))
            number = connection.execute(
                "SELECT COALESCE(MAX(version_number),0)+1 FROM document_versions WHERE workspace_id=? AND document_id=?",
                (workspace_id, document_id)).fetchone()[0]
            if number > MAX_VERSIONS:
                raise SupportOpsError("CAPACITY_EXCEEDED")
            version_id, job_id, now = new_id("ver"), new_id("job"), utc_now()
            connection.execute("INSERT INTO document_versions VALUES(?,?,?,?,?,?,?,?)", (
                version_id, workspace_id, document_id, number, digest,
                f"sqlite-originals/{workspace_id}/{document_id}/{version_id}", "pending", now))
            self._originals.put(connection, workspace_id, document_id, version_id,
                                request.filename, request.title, content)
            connection.execute("INSERT INTO ingestion_jobs VALUES(?,?,?,?,?,?,?,?,?,?)", (
                job_id, workspace_id, document_id, version_id, request.idempotency_key, fingerprint,
                "pending", 0, None, now))
            self._database.record_audit(connection, workspace_id, identity.subject, "document.enqueued", document_id)
            return self._job_view(self._job(connection, workspace_id, job_id))

    def get_document(self, credential, workspace_id, document_id) -> DocumentView:
        with self._scope(credential, workspace_id) as (connection, _):
            return self._view(connection, workspace_id, document_id)

    def list_documents(self, credential, workspace_id, *, limit=100) -> list[DocumentView]:
        with self._scope(credential, workspace_id) as (connection, _):
            limit = self._access._limit(limit)
            rows = connection.execute(
                "SELECT id FROM documents d WHERE workspace_id=? AND NOT EXISTS "
                "(SELECT 1 FROM document_tombstones t WHERE t.workspace_id=d.workspace_id AND t.document_id=d.id) "
                "ORDER BY id LIMIT ?", (workspace_id, limit)).fetchall()
            return [self._view(connection, workspace_id, row["id"]) for row in rows]

    def get_job(self, credential, workspace_id, job_id) -> JobView:
        with self._scope(credential, workspace_id) as (connection, _):
            return self._job_view(self._job(connection, workspace_id, job_id))

    def list_jobs(self, credential, workspace_id, *, limit=100) -> list[JobView]:
        with self._scope(credential, workspace_id) as (connection, _):
            limit = self._access._limit(limit)
            rows = connection.execute("SELECT * FROM ingestion_jobs WHERE workspace_id=? ORDER BY created_at DESC,id LIMIT ?",
                                      (workspace_id, limit)).fetchall()
            return [self._job_view(row) for row in rows]

    def retry_job(self, credential, workspace_id, job_id) -> JobView:
        with self._scope(credential, workspace_id, write=True) as (connection, identity):
            job = self._job(connection, workspace_id, job_id)
            doc = self._document(connection, workspace_id, job["document_id"])
            if job["state"] != "failed" or job["attempts"] >= MAX_ATTEMPTS:
                raise SupportOpsError("CONFLICT")
            newer = connection.execute(
                "SELECT 1 FROM document_versions cur JOIN document_versions retry "
                "ON retry.workspace_id=cur.workspace_id AND retry.document_id=cur.document_id "
                "WHERE cur.workspace_id=? AND cur.document_id=? AND cur.id=? AND retry.id=? "
                "AND cur.version_number>retry.version_number",
                (workspace_id, job["document_id"], doc["current_version_id"], job["version_id"])).fetchone()
            if newer:
                raise SupportOpsError("CONFLICT")
            connection.execute("UPDATE ingestion_jobs SET state='pending',error_code=NULL WHERE workspace_id=? AND id=?",
                               (workspace_id, job_id))
            connection.execute("UPDATE document_versions SET state='pending' WHERE workspace_id=? AND document_id=? AND id=?",
                               (workspace_id, job["document_id"], job["version_id"]))
            self._database.record_audit(connection, workspace_id, identity.subject, "index.retried", job_id)
            return self._job_view(self._job(connection, workspace_id, job_id))

    def _claim(self, credential, workspace_id):
        with self._scope(credential, workspace_id, write=True) as (connection, identity):
            now = self._clock()
            expired = connection.execute(
                "SELECT j.* FROM ingestion_jobs j JOIN job_leases l ON l.workspace_id=j.workspace_id AND l.job_id=j.id "
                "WHERE j.workspace_id=? AND j.state='running' AND l.expires_at<=?", (workspace_id, now)).fetchall()
            for job in expired:
                state = "failed" if job["attempts"] >= MAX_ATTEMPTS else "pending"
                connection.execute("UPDATE ingestion_jobs SET state=?,error_code='LEASE_EXPIRED' WHERE workspace_id=? AND id=?",
                                   (state, workspace_id, job["id"]))
                if state == "failed":
                    connection.execute("UPDATE document_versions SET state='failed' WHERE workspace_id=? AND document_id=? AND id=?",
                                       (workspace_id, job["document_id"], job["version_id"]))
                connection.execute("DELETE FROM job_leases WHERE workspace_id=? AND job_id=?", (workspace_id, job["id"]))
                self._database.record_audit(connection, workspace_id, identity.subject, "index.recovered", job["id"])
            job = connection.execute(
                "SELECT * FROM ingestion_jobs j WHERE workspace_id=? AND state='pending' "
                "AND NOT EXISTS(SELECT 1 FROM document_tombstones t WHERE t.workspace_id=j.workspace_id "
                "AND t.document_id=j.document_id) ORDER BY created_at,id LIMIT 1", (workspace_id,)).fetchone()
            if job is None:
                return None
            token = new_id("lease")
            connection.execute("UPDATE ingestion_jobs SET state='running',attempts=attempts+1,error_code=NULL "
                               "WHERE workspace_id=? AND id=?", (workspace_id, job["id"]))
            connection.execute("INSERT INTO job_leases VALUES(?,?,?,?)",
                               (workspace_id, job["id"], token, now + self._lease_seconds))
            return dict(job), token

    def process_next(self, credential, workspace_id) -> JobView | None:
        """显式处理一个任务；不是常驻服务。模型/索引计算不占用 SQLite 写事务。"""
        claimed = self._claim(credential, workspace_id)
        if claimed is None:
            return None
        job, token = claimed
        error, entries = None, []
        try:
            with self._scope(credential, workspace_id) as (connection, _):
                self._document(connection, workspace_id, job["document_id"])
                original = self._originals.get(connection, workspace_id, job["document_id"], job["version_id"])
                version = connection.execute("SELECT content_sha256 FROM document_versions "
                    "WHERE workspace_id=? AND document_id=? AND id=?",
                    (workspace_id, job["document_id"], job["version_id"])).fetchone()
            if hashlib.sha256(original["content"]).hexdigest() != version["content_sha256"]:
                raise ValueError("original checksum mismatch")
            text = decode_document(original["content"])
        except SupportOpsError:
            raise
        except Exception:
            error = "ORIGINAL_UNAVAILABLE"
        if error is None:
            try:
                document = Document(id=job["document_id"], title=original["title"], content=text,
                    source=f"supportops/{workspace_id}/{job['document_id']}/{job['version_id']}",
                    metadata={"workspace_id": workspace_id, "version_id": job["version_id"]})
                entries = self._validate_entries(document, self._index.build(document))
            except Exception:
                error = "INDEX_BUILD_FAILED"
        return self._finish(credential, workspace_id, job["id"], token, entries, error)

    def _validate_entries(self, document, entries):
        if not isinstance(entries, list) or not 1 <= len(entries) <= 500:
            raise ValueError("invalid index output")
        validated = []
        for entry in entries:
            if not isinstance(entry, IndexEntry):
                raise ValueError("invalid index entry")
            chunk = entry.chunk
            if (chunk.document_id != document.id or chunk.source != document.source or chunk.title != document.title
                or chunk.metadata != {**document.metadata, "document_version":
                    hashlib.sha256(document.content.encode("utf-8")).hexdigest()[:24]}
                or document.content[chunk.start_char:chunk.end_char] != chunk.content):
                raise ValueError("index evidence mismatch")
            if len(entry.vector) != self._index.dimension:
                raise ValueError("index dimension mismatch")
            vector = VectorRecord(id=chunk.id, vector=entry.vector).vector
            scoped_id = "chunk-" + hashlib.sha256(f"{document.source}/{chunk.id}".encode()).hexdigest()
            validated.append(IndexEntry(chunk=chunk.model_copy(update={"id": scoped_id}), vector=vector))
        if len({entry.chunk.id for entry in validated}) != len(validated):
            raise ValueError("duplicate index entries")
        return validated

    def _finish(self, credential, workspace_id, job_id, token, entries, error):
        with self._scope(credential, workspace_id, write=True) as (connection, identity):
            job = self._job(connection, workspace_id, job_id)
            lease = connection.execute("SELECT token,expires_at FROM job_leases WHERE workspace_id=? AND job_id=?",
                                       (workspace_id, job_id)).fetchone()
            if job["state"] != "running" or lease is None or lease["token"] != token or lease["expires_at"] <= self._clock():
                raise SupportOpsError("CONFLICT")
            doc = self._document(connection, workspace_id, job["document_id"])
            versions = connection.execute("SELECT id,version_number FROM document_versions WHERE workspace_id=? AND document_id=?",
                                          (workspace_id, job["document_id"])).fetchall()
            numbers = {row["id"]: row["version_number"] for row in versions}
            superseded = doc["current_version_id"] is not None and numbers[doc["current_version_id"]] > numbers[job["version_id"]]
            state = "cancelled" if superseded else "failed" if error else "succeeded"
            if superseded:
                error = "SUPERSEDED"
            if state == "succeeded":
                for entry in entries:
                    connection.execute("INSERT INTO indexed_chunks VALUES(?,?,?,?,?,?)", (
                        workspace_id, job["document_id"], job["version_id"], entry.chunk.id,
                        self._index.profile, entry.model_dump_json()))
                title = entries[0].chunk.title
                connection.execute("UPDATE documents SET current_version_id=?,title=? WHERE workspace_id=? AND id=?",
                                   (job["version_id"], title, workspace_id, job["document_id"]))
            connection.execute("UPDATE document_versions SET state=? WHERE workspace_id=? AND document_id=? AND id=?",
                               ("ready" if state == "succeeded" else "failed", workspace_id, job["document_id"], job["version_id"]))
            connection.execute("UPDATE ingestion_jobs SET state=?,error_code=? WHERE workspace_id=? AND id=?",
                               (state, error, workspace_id, job_id))
            connection.execute("DELETE FROM job_leases WHERE workspace_id=? AND job_id=?", (workspace_id, job_id))
            self._database.record_audit(connection, workspace_id, identity.subject, f"index.{state}", job_id)
            return self._job_view(self._job(connection, workspace_id, job_id))

    def delete_document(self, credential, workspace_id, document_id) -> dict:
        with self._scope(credential, workspace_id, write=True) as (connection, identity):
            # 重复删除返回相同结果，但仍然先检查当前身份与空间角色。
            tombstone = connection.execute("SELECT 1 FROM document_tombstones WHERE workspace_id=? AND document_id=?",
                                           (workspace_id, document_id)).fetchone()
            if not tombstone:
                self._document(connection, workspace_id, document_id)
                connection.execute("INSERT INTO document_tombstones VALUES(?,?,?)", (workspace_id, document_id, utc_now()))
                connection.execute("UPDATE documents SET current_version_id=NULL,title='[deleted]' WHERE workspace_id=? AND id=?",
                                   (workspace_id, document_id))
                connection.execute("UPDATE document_versions SET state='deleted' WHERE workspace_id=? AND document_id=?",
                                   (workspace_id, document_id))
                connection.execute("DELETE FROM indexed_chunks WHERE workspace_id=? AND document_id=?", (workspace_id, document_id))
                self._originals.delete(connection, workspace_id, document_id)
                connection.execute("DELETE FROM job_leases WHERE workspace_id=? AND job_id IN "
                    "(SELECT id FROM ingestion_jobs WHERE workspace_id=? AND document_id=?)", (workspace_id, workspace_id, document_id))
                connection.execute("UPDATE ingestion_jobs SET state='cancelled',error_code='DOCUMENT_DELETED' "
                    "WHERE workspace_id=? AND document_id=? AND state IN ('pending','running','failed')", (workspace_id, document_id))
                self._database.record_audit(connection, workspace_id, identity.subject, "document.deleted", document_id)
            return {"document_id": document_id, "state": "deleted"}

    def search(self, credential, workspace_id, question, *, limit=5) -> DocumentRAGResult:
        with self._scope(credential, workspace_id) as (connection, _):
            if not isinstance(question, str) or not question.strip() or len(question) > 2000:
                raise SupportOpsError("INVALID_REQUEST")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
                raise SupportOpsError("INVALID_REQUEST")
            rows = connection.execute(
                "SELECT c.* FROM indexed_chunks c JOIN documents d ON d.workspace_id=c.workspace_id "
                "AND d.id=c.document_id AND d.current_version_id=c.version_id "
                "JOIN document_versions v ON v.workspace_id=c.workspace_id AND v.document_id=c.document_id AND v.id=c.version_id "
                "WHERE c.workspace_id=? AND v.state='ready' AND NOT EXISTS(SELECT 1 FROM document_tombstones t "
                "WHERE t.workspace_id=c.workspace_id AND t.document_id=c.document_id) LIMIT ?",
                (workspace_id, MAX_SEARCH_CHUNKS + 1)).fetchall()
            if len(rows) > MAX_SEARCH_CHUNKS:
                raise SupportOpsError("CAPACITY_EXCEEDED")
            try:
                if any(row["profile"] != self._index.profile for row in rows):
                    raise ValueError("index profile mismatch")
                entries = [self._decode_entry(row, workspace_id) for row in rows]
                result = self._index.query(question.strip(), entries, limit=limit)
                self._verify_result(result, question.strip(), entries, limit)
                return result
            except Exception:
                raise SupportOpsError("INDEX_UNAVAILABLE") from None

    @staticmethod
    def _decode_entry(row, workspace_id):
        entry = IndexEntry.model_validate_json(row["data_json"])
        if (entry.chunk.id != row["chunk_id"] or entry.chunk.document_id != row["document_id"]
            or entry.chunk.metadata.get("workspace_id") != workspace_id
            or entry.chunk.metadata.get("version_id") != row["version_id"]
            or entry.chunk.source != f"supportops/{workspace_id}/{row['document_id']}/{row['version_id']}"):
            raise ValueError("index scope mismatch")
        return entry

    @staticmethod
    def _verify_result(result, question, entries, limit):
        if not isinstance(result, DocumentRAGResult) or result.query != question or len(result.hits) > limit:
            raise ValueError("invalid search output")
        if len(result.hits) != len(result.citations) or len({hit.chunk.id for hit in result.hits}) != len(result.hits):
            raise ValueError("invalid citations")
        allowed = {entry.chunk.id: entry.chunk for entry in entries}
        for number, (hit, citation) in enumerate(zip(result.hits, result.citations, strict=True), 1):
            if allowed.get(hit.chunk.id) != hit.chunk:
                raise ValueError("unscoped evidence")
            expected = Citation(label=f"D{number}", chunk_id=hit.chunk.id, **{
                field: getattr(hit.chunk, field) for field in (
                    "document_id", "source", "title", "chunk_index", "section", "page_number", "start_char", "end_char")})
            if citation != expected:
                raise ValueError("citation mismatch")

    def read_citation(self, credential, workspace_id, document_id, version_id, chunk_id) -> dict:
        with self._scope(credential, workspace_id) as (connection, _):
            doc = self._document(connection, workspace_id, document_id)
            row = connection.execute(
                "SELECT c.* FROM indexed_chunks c JOIN document_versions v "
                "ON v.workspace_id=c.workspace_id AND v.document_id=c.document_id AND v.id=c.version_id "
                "WHERE c.workspace_id=? AND c.document_id=? AND c.version_id=? AND c.chunk_id=? AND v.state='ready'",
                (workspace_id, document_id, version_id, chunk_id)).fetchone()
            if row is None:
                raise SupportOpsError("NOT_FOUND")
            try:
                chunk = self._decode_entry(row, workspace_id).chunk
            except ValueError:
                raise SupportOpsError("INDEX_UNAVAILABLE") from None
            return {"version_id": version_id, "is_current": doc["current_version_id"] == version_id,
                    "chunk": chunk.model_dump(mode="json")}


__all__ = ["DocumentService"]
