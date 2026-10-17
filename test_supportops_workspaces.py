"""P1：本地 SQLite 与 Fake/演示身份，验证持久化、权限和数据库约束。"""

import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from supportops import ApplicationDatabase, DemoIdentityProvider, SupportOpsError, WorkspaceService
from supportops.__main__ import main
from supportops.database import APPLICATION_ID, SCHEMA_VERSION
from supportops.models import Identity


class FakeIdentityProvider:
    def authenticate(self, credential):
        if credential != "verified-fixture":
            raise SupportOpsError("UNAUTHENTICATED")
        return Identity(subject="demo-reader", auth_mode="external")


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "app.sqlite3"
        self.database = ApplicationDatabase(self.path)
        self.addCleanup(self.database.close)
        self.database.bootstrap_demo(enabled=True)
        self.provider = DemoIdentityProvider(enabled=True)
        self.service = WorkspaceService(self.database, self.provider)

    def assert_error(self, code, action):
        with self.assertRaises(SupportOpsError) as context:
            action()
        self.assertEqual(context.exception.code, code)
        return context.exception


class TestWorkspaceIdentityAndAccess(WorkspaceFixture):
    def test_demo_requires_explicit_opt_in(self):
        self.assert_error("DEMO_DISABLED", DemoIdentityProvider)
        self.assert_error("DEMO_DISABLED", self.database.bootstrap_demo)

    def test_missing_unknown_or_user_id_is_not_a_credential(self):
        for credential in (None, "", "demo-admin", "administrator", {"user_id": "demo-admin"}):
            with self.subTest(credential=credential):
                self.assert_error("UNAUTHENTICATED", lambda: self.service.list_workspaces(credential))

    def test_lists_only_live_memberships(self):
        self.assertEqual([row.id for row in self.service.list_workspaces("reader")], ["helloagent"])
        self.assertEqual([row.id for row in self.service.list_workspaces("admin")], ["helloagent", "internal-lab"])
        self.assertEqual(self.service.get_workspace("reader", "helloagent").role, "reader")

    def test_hidden_and_nonexistent_workspace_have_same_error(self):
        hidden = self.assert_error("NOT_FOUND", lambda: self.service.get_workspace("reader", "internal-lab"))
        absent = self.assert_error("NOT_FOUND", lambda: self.service.get_workspace("reader", "missing"))
        self.assertEqual(hidden.to_dict(), absent.to_dict())
        self.assertEqual(hidden.status, 404)

    def test_all_workspace_entrypoints_reject_cross_space_access(self):
        operations = [lambda: self.service.list_members("reader", "internal-lab"),
                      lambda: self.service.list_audit("reader", "internal-lab"),
                      lambda: self.service.set_member("reader", "internal-lab", {"member_id": "demo-reader", "role": "admin"}),
                      lambda: self.service.remove_member("reader", "internal-lab", "demo-admin")]
        for operation in operations:
            self.assert_error("NOT_FOUND", operation)

    def test_readers_cannot_manage_members_or_read_audit(self):
        operations = [lambda: self.service.list_members("reader", "helloagent"),
                      lambda: self.service.list_audit("reader", "helloagent"),
                      lambda: self.service.set_member("reader", "helloagent", {"member_id": "demo-reader", "role": "admin"}),
                      lambda: self.service.remove_member("reader", "helloagent", "demo-admin")]
        for operation in operations:
            self.assert_error("FORBIDDEN", operation)

    def test_editor_role_is_not_an_admin(self):
        self.service.set_member("admin", "helloagent", {"member_id": "demo-reader", "role": "editor"})
        self.assertEqual(self.service.get_workspace("reader", "helloagent").role, "editor")
        self.assert_error("FORBIDDEN", lambda: self.service.list_members("reader", "helloagent"))

    def test_role_is_per_workspace_not_global(self):
        self.service.set_member("admin", "helloagent", {"member_id": "demo-reader", "role": "admin"})
        self.assertEqual(len(self.service.list_members("reader", "helloagent")), 2)
        self.assert_error("NOT_FOUND", lambda: self.service.get_workspace("reader", "internal-lab"))

    def test_extra_actor_owner_or_role_fields_cannot_impersonate(self):
        for extra in ({"user_id": "demo-admin"}, {"owner_id": "demo-admin"}, {"role": "admin"}):
            with self.subTest(extra=extra):
                self.assert_error("INVALID_REQUEST", lambda: self.service.create_workspace("reader", {"name": "test", **extra}))
        self.assert_error("INVALID_REQUEST", lambda: self.service.set_member("admin", "helloagent", {
            "member_id": "demo-reader", "role": "editor", "user_id": "demo-admin"}))
        self.assertEqual(self.service.get_workspace("reader", "helloagent").role, "reader")

    def test_revocation_is_immediate_and_reinitializing_does_not_restore_it(self):
        self.service.remove_member("admin", "helloagent", "demo-reader")
        self.assertEqual(self.service.list_workspaces("reader"), [])
        self.assertFalse(self.database.bootstrap_demo(enabled=True))
        self.assert_error("NOT_FOUND", lambda: self.service.get_workspace("reader", "helloagent"))

    def test_disabled_or_unprovisioned_account_cannot_use_identity(self):
        with self.database.transaction(write=True) as connection:
            connection.execute("UPDATE users SET active=0 WHERE id='demo-reader'")
        self.assert_error("UNAUTHENTICATED", lambda: self.service.list_workspaces("reader"))
        with ApplicationDatabase() as empty:
            self.assert_error("UNAUTHENTICATED", lambda: WorkspaceService(empty, self.provider).list_workspaces("admin"))

    def test_last_active_admin_cannot_be_removed_or_demoted(self):
        self.assert_error("CONFLICT", lambda: self.service.remove_member("admin", "helloagent", "demo-admin"))
        self.assert_error("CONFLICT", lambda: self.service.set_member("admin", "helloagent", {"member_id": "demo-admin", "role": "reader"}))
        self.assertEqual(len(self.service.list_members("admin", "helloagent")), 2)

    def test_can_transfer_admin_but_new_admin_has_no_other_space_privilege(self):
        self.service.set_member("admin", "helloagent", {"member_id": "demo-reader", "role": "admin"})
        self.service.set_member("admin", "helloagent", {"member_id": "demo-admin", "role": "reader"})
        self.assert_error("FORBIDDEN", lambda: self.service.list_members("admin", "helloagent"))
        self.assertEqual(len(self.service.list_members("reader", "helloagent")), 2)
        self.assert_error("NOT_FOUND", lambda: self.service.list_members("reader", "internal-lab"))

    def test_injected_verified_provider_works_without_demo_credential(self):
        service = WorkspaceService(self.database, FakeIdentityProvider())
        self.assertEqual(service.list_workspaces("verified-fixture")[0].role, "reader")
        self.assert_error("UNAUTHENTICATED", lambda: service.list_workspaces("reader"))

    def test_limits_and_input_validation_are_strict(self):
        for limit in (0, True, 201, "10"):
            self.assert_error("INVALID_REQUEST", lambda: self.service.list_workspaces("admin", limit=limit))
        for name in (" ", 123, "x" * 201):
            self.assert_error("INVALID_REQUEST", lambda: self.service.create_workspace("admin", {"name": name}))
        for role in ("owner", "ADMIN", True):
            self.assert_error("INVALID_REQUEST", lambda: self.service.set_member("admin", "helloagent", {"member_id": "demo-reader", "role": role}))
        self.assert_error("NOT_FOUND", lambda: self.service.get_workspace("admin", "' OR 1=1 --"))


class TestWorkspacePersistence(WorkspaceFixture):
    def test_workspace_members_and_audit_survive_reopen(self):
        workspace = self.service.create_workspace("reader", {"name": " 新项目 "})
        self.service.set_member("reader", workspace.id, {"member_id": "demo-admin", "role": "editor"})
        self.database.close()
        with ApplicationDatabase(self.path) as reopened:
            service = WorkspaceService(reopened, self.provider)
            self.assertEqual(service.get_workspace("admin", workspace.id).role, "editor")
            self.assertEqual(service.get_workspace("reader", workspace.id).name, "新项目")
            audit = service.list_audit("reader", workspace.id)
            self.assertEqual({row.action for row in audit}, {"workspace.created", "membership.changed"})
            self.assertTrue(all(row.workspace_id == workspace.id for row in audit))
            self.assert_error("FORBIDDEN", lambda: service.list_audit("admin", workspace.id))

    def test_idempotent_member_changes_and_removal_do_not_duplicate_audit(self):
        before = len(self.service.list_audit("admin", "helloagent"))
        self.service.set_member("admin", "helloagent", {"member_id": "demo-reader", "role": "reader"})
        self.assertEqual(len(self.service.list_audit("admin", "helloagent")), before)
        self.service.remove_member("admin", "helloagent", "demo-reader")
        self.service.remove_member("admin", "helloagent", "demo-reader")
        self.assertEqual(len(self.service.list_audit("admin", "helloagent")), before + 1)

    def test_audit_failure_rolls_back_workspace_and_membership(self):
        before = self.service.list_workspaces("admin")
        with patch.object(self.database, "record_audit", side_effect=sqlite3.OperationalError("fake private details")):
            error = self.assert_error("STORAGE_UNAVAILABLE", lambda: self.service.create_workspace("admin", {"name": "must rollback"}))
        self.assertNotIn("private", str(error))
        self.assertEqual(before, self.service.list_workspaces("admin"))

    def test_audit_failure_also_rolls_back_permission_change(self):
        with patch.object(self.database, "record_audit", side_effect=sqlite3.OperationalError("private details")):
            self.assert_error("STORAGE_UNAVAILABLE", lambda: self.service.set_member(
                "admin", "helloagent", {"member_id": "demo-reader", "role": "admin"}))
        self.assertEqual(self.service.get_workspace("reader", "helloagent").role, "reader")

    def test_nested_transaction_rejection_does_not_rollback_outer_work(self):
        with self.database.transaction(write=True) as connection:
            connection.execute("UPDATE workspaces SET name='outer work' WHERE id='helloagent'")
            def nested():
                with self.database.transaction():
                    self.fail("嵌套事务应被拒绝")
            self.assert_error("STORAGE_UNAVAILABLE", nested)
        self.assertEqual(self.service.get_workspace("admin", "helloagent").name, "outer work")

    def test_foreign_keys_role_and_member_uniqueness_are_enforced(self):
        statements = [
            ("INSERT INTO memberships VALUES(?,?,?,?)", ("missing", "demo-reader", "reader", "now")),
            ("INSERT INTO memberships VALUES(?,?,?,?)", ("helloagent", "missing", "reader", "now")),
            ("INSERT INTO memberships VALUES(?,?,?,?)", ("helloagent", "demo-reader", "admin", "now")),
            ("UPDATE memberships SET role=? WHERE user_id=?", ("superuser", "demo-reader")),
        ]
        for sql, args in statements:
            with self.subTest(sql=sql):
                def write():
                    with self.database.transaction(write=True) as connection:
                        connection.execute(sql, args)
                self.assert_error("CONFLICT", write)

    def test_document_version_and_job_composite_keys_prevent_cross_space_links(self):
        with self.database.transaction(write=True) as connection:
            for workspace in ("helloagent", "internal-lab"):
                connection.execute("INSERT INTO documents(id,workspace_id,title,current_version_id,created_by,created_at) VALUES(?,?,?,?,?,?)",
                                   ("doc-1", workspace, "title", None, "demo-admin", "now"))
            connection.execute("INSERT INTO document_versions(id,workspace_id,document_id,version_number,content_sha256,object_key,state,created_at) VALUES(?,?,?,?,?,?,?,?)",
                               ("version-1", "helloagent", "doc-1", 1, "a" * 64, "object-1", "ready", "now"))
        def cross_job():
            with self.database.transaction(write=True) as connection:
                connection.execute("INSERT INTO ingestion_jobs(id,workspace_id,document_id,version_id,idempotency_key,request_sha256,state,attempts,error_code,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                   ("job-1", "internal-lab", "doc-1", "version-1", "idem", "b" * 64, "pending", 0, None, "now"))
        # 使用显式字段，确保约束测试不因参数数量错误而误报。
        def cross_version():
            with self.database.transaction(write=True) as connection:
                connection.execute("UPDATE documents SET current_version_id='version-1' WHERE workspace_id='internal-lab'")
        self.assert_error("CONFLICT", cross_job)
        self.assert_error("CONFLICT", cross_version)
        with self.database.transaction() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_schema_identification_and_unsupported_version_fail_closed(self):
        with self.database.transaction() as connection:
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.database.close()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA user_version=999")
        self.assert_error("STORAGE_VERSION_UNSUPPORTED", lambda: ApplicationDatabase(self.path))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 999)
            self.assertEqual(connection.execute("SELECT count(*) FROM users").fetchone()[0], 2)

    def test_unrelated_database_is_not_repurposed(self):
        path = Path(self.directory.name) / "other.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE unrelated(value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES('keep')")
            connection.commit()
        self.assert_error("STORAGE_VERSION_UNSUPPORTED", lambda: ApplicationDatabase(path))
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM unrelated").fetchone()[0], "keep")
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], 0)

    def test_closed_database_exposes_only_safe_error(self):
        self.database.close()
        self.assert_error("STORAGE_UNAVAILABLE", lambda: self.service.list_workspaces("reader"))
        self.database.close()

    def test_view_only_foreign_database_is_not_initialized(self):
        path = Path(self.directory.name) / "view-only.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE VIEW unrelated_view AS SELECT 1 AS value")
        self.assert_error("STORAGE_VERSION_UNSUPPORTED", lambda: ApplicationDatabase(path))

    def test_audit_failure_cannot_leave_partial_demo_bootstrap(self):
        with ApplicationDatabase() as database:
            with patch.object(database, "record_audit", side_effect=sqlite3.OperationalError("fake bootstrap error")):
                self.assert_error("STORAGE_UNAVAILABLE", lambda: database.bootstrap_demo(enabled=True))
            with database.transaction() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM users").fetchone()[0], 0)
            self.assertTrue(database.bootstrap_demo(enabled=True))

    def test_demo_bootstrap_refuses_to_merge_into_occupied_application(self):
        with ApplicationDatabase() as database:
            with database.transaction(write=True) as connection:
                connection.execute("INSERT INTO users VALUES('existing','existing',1,'now')")
            self.assert_error("CONFLICT", lambda: database.bootstrap_demo(enabled=True))
            with database.transaction() as connection:
                self.assertEqual(connection.execute("SELECT id FROM users").fetchone()[0], "existing")

    def test_job_idempotency_is_workspace_scoped_and_metadata_constraints_hold(self):
        with self.database.transaction(write=True) as connection:
            for workspace in ("helloagent", "internal-lab"):
                connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?)",
                                   ("doc-1", workspace, "title", None, "demo-admin", "now"))
                connection.execute("INSERT INTO document_versions VALUES(?,?,?,?,?,?,?,?)",
                                   ("version-1", workspace, "doc-1", 1, "a" * 64, "object-1", "pending", "now"))
                connection.execute("INSERT INTO ingestion_jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
                                   ("job-1", workspace, "doc-1", "version-1", "same-key", "b" * 64, "pending", 0, None, "now"))
        bad_statements = [
            ("INSERT INTO ingestion_jobs VALUES(?,?,?,?,?,?,?,?,?,?)",
             ("job-2", "helloagent", "doc-1", "version-1", "same-key", "c" * 64, "pending", 0, None, "now")),
            ("INSERT INTO document_versions VALUES(?,?,?,?,?,?,?,?)",
             ("version-2", "helloagent", "doc-1", 1, "c" * 64, "object-2", "pending", "now")),
            ("UPDATE ingestion_jobs SET state=?", ("unknown",)),
            ("UPDATE ingestion_jobs SET attempts=?", (-1,)),
        ]
        for sql, args in bad_statements:
            def write():
                with self.database.transaction(write=True) as connection:
                    connection.execute(sql, args)
            self.assert_error("CONFLICT", write)

    def test_two_connections_cannot_both_demote_last_admin(self):
        self.service.set_member("admin", "helloagent", {"member_id": "demo-reader", "role": "admin"})
        barrier = Barrier(2)
        def demote(profile, member):
            with ApplicationDatabase(self.path) as database:
                service = WorkspaceService(database, self.provider)
                barrier.wait(timeout=5)
                try:
                    service.set_member(profile, "helloagent", {"member_id": member, "role": "reader"})
                    return "changed"
                except SupportOpsError as error:
                    return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(demote, "admin", "demo-admin")
            second = pool.submit(demote, "reader", "demo-reader")
            outcomes = [first.result(), second.result()]
        self.assertCountEqual(outcomes, ["changed", "CONFLICT"])
        with self.database.transaction() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM memberships WHERE workspace_id='helloagent' AND role='admin'").fetchone()[0], 1)


class TestWorkspaceDemoCLI(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "outputs" / "demo.sqlite3"
        self.file_patch = patch("supportops.__main__.__file__", str(self.root / "supportops" / "__main__.py"))
        self.file_patch.start()
        self.addCleanup(self.file_patch.stop)

    def invoke(self, *args, enabled=True, profile="admin"):
        output, error = io.StringIO(), io.StringIO()
        command = ["demo", "--database", str(self.path), "--profile", profile]
        if enabled:
            command.append("--enable-demo")
        with redirect_stdout(output), redirect_stderr(error):
            code = main([*command, *args])
        return code, output.getvalue(), error.getvalue()

    def test_disabled_demo_does_not_create_database(self):
        code, _, error = self.invoke("init", enabled=False)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(error)["code"], "DEMO_DISABLED")
        self.assertFalse(self.path.exists())

    def test_init_persists_and_cli_lists_scoped_spaces_with_demo_warning(self):
        self.assertEqual(self.invoke("init")[0], 0)
        code, output, _ = self.invoke("workspaces", profile="reader")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertIn("非企业认证", payload["warning"])
        self.assertEqual([row["id"] for row in payload["result"]], ["helloagent"])
        code, _, error = self.invoke("workspace", "internal-lab", profile="reader")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(error)["code"], "NOT_FOUND")

    def test_rejects_database_outside_outputs(self):
        self.path = self.root / "protected.sqlite3"
        code, _, error = self.invoke("init")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(error)["code"], "INVALID_REQUEST")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
