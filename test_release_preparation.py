"""Release tooling tests: isolated source trees/Git indexes, no real credentials."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.check_release import ROOT, OWNER_LICENSE_DECISION, allowed, audit, collect, export, findings_for


class ReleasePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.put("README.md", "# Public demo\n")

    def put(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, check=True)
        return result.stdout

    def init_git(self):
        if not shutil.which("git"):
            self.skipTest("git unavailable")
        self.git("init", "--quiet")

    def test_allowlist_keeps_public_sources(self):
        for name in ["README.md", "hello_agents/core/agent.py", "supportops/web/dist/app.js",
                     "data/supportops/corpus/manifest.json", "test_supportops.py",
                     "configs/supportops/pilot.example.json", ".github/workflows/ci.yml",
                     "supportops.env.example", "docs/assets/supportops-documents.png"]:
            with self.subTest(name=name):
                self.assertTrue(allowed(name))

    def test_allowlist_rejects_private_and_unreviewed_paths(self):
        for name in [".env", ".env.supportops", ".env.supportops.txt", "outputs/report.json",
                     ".idea/workspace.xml", "HANDOFF.md", "data/customer/records.json",
                     "configs/supportops/pilot.json", "docs/credentials.json", "key.pem",
                     "docs/.env/notes.md", "docs/assets/unreviewed.png", "../README.md",
                     "docs/../../private.py", "/secret.py", "C:/secret.py", "docs\\secret.md"]:
            with self.subTest(name=name):
                self.assertFalse(allowed(name))

    def test_never_opens_real_env_or_outputs(self):
        self.put(".env.supportops", "do not read")
        self.put("outputs/private.md", "do not read")
        self.put("docs/.env/private.md", "do not read")
        original = Path.read_bytes
        opened = []
        def read(path):
            name = path.relative_to(self.root).as_posix()
            self.assertTrue(allowed(name))
            opened.append(name)
            return original(path)
        with patch.object(Path, "read_bytes", read):
            files, issues = collect(self.root)
        self.assertEqual(set(files), {"README.md"})
        self.assertEqual(opened, ["README.md"])
        self.assertFalse(issues)

    def test_secret_findings_contain_only_location_and_rule(self):
        secret = "sk-" + "A1b2C3d4" * 6
        self.put("main.py", 'value = "' + secret + '"\n')
        report, _ = audit(self.root)
        self.assertFalse(report["candidate_scan_passed"])
        self.assertNotIn(secret, json.dumps(report))
        self.assertEqual(report["findings"][0], {
            "path": "main.py", "origin": "working_tree", "line": 1, "rule": "provider_token"})

    def test_private_keys_and_personal_mail_are_flagged(self):
        key_header = "-----BEGIN " + "PRIVATE KEY-----"
        mail = "private.user" + "@qq.com"
        self.assertTrue(findings_for("example.py", key_header.encode(), "working_tree"))
        self.assertTrue(findings_for("email.env.example", mail.encode(), "working_tree"))
        self.assertFalse(findings_for("email.env.example", b"your_qq_number@qq.com", "working_tree"))

    def test_literal_credentials_scanned_even_in_examples(self):
        value = "AbCdEfGh" + "12345678ijklmnop"
        source = 'api_key="' + value + '"'
        findings = findings_for("supportops.env.example", source.encode(), "working_tree")
        self.assertEqual(findings[0]["rule"], "credential_literal")
        self.assertNotIn(value, json.dumps(findings))
        self.assertFalse(findings_for("supportops.env.example",
                                     b'api_key="replace_with_your_key"', "working_tree"))
        self.assertTrue(findings_for("supportops.env.example",
                                    ("LLM_API_KEY=" + value).encode(), "working_tree"))

    def test_linked_tree_guard_without_symlink_privilege(self):
        self.put("docs/README.md", "should not open")
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda p: p == self.root / "docs" or original(p)):
            report, files = audit(self.root)
        self.assertFalse(report["candidate_scan_passed"])
        self.assertNotIn("docs/README.md", files)

    def test_manifest_is_sorted_stable_and_hashes_exact_bytes(self):
        self.put("supportops/example.py", "# public\n")
        first, files = audit(self.root)
        second, _ = audit(self.root)
        self.assertEqual(first, second)
        self.assertEqual([f["path"] for f in first["files"]], sorted(files))
        for row in first["files"]:
            self.assertEqual(row["sha256"], hashlib.sha256(files[row["path"]]).hexdigest())

    def test_license_file_alone_cannot_mark_public_ready(self):
        self.put("LICENSE", "Needs source review\n")
        report, _ = audit(self.root)
        self.assertTrue(report["candidate_scan_passed"])
        self.assertFalse(report["public_release_ready"])
        self.assertIn("pending", report["license_review"])

    def test_confirmed_license_and_clean_index_clear_release_gate(self):
        self.init_git()
        self.root.joinpath("LICENSE").write_bytes((ROOT / "LICENSE").read_bytes())
        self.put("THIRD_PARTY_NOTICES.md", OWNER_LICENSE_DECISION.decode() + "\n")
        self.git("add", "--", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md")
        report, _ = audit(self.root)
        self.assertTrue(report["public_release_ready"])
        self.assertEqual(report["license_review"], "owner_confirmed_CC_BY_NC_SA_4_0")

    def test_changed_license_cannot_clear_release_gate(self):
        self.init_git()
        self.root.joinpath("LICENSE").write_bytes((ROOT / "LICENSE").read_bytes() + b"Changed\n")
        self.put("THIRD_PARTY_NOTICES.md", OWNER_LICENSE_DECISION.decode() + "\n")
        self.git("add", "--", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md")
        report, _ = audit(self.root)
        self.assertFalse(report["public_release_ready"])

    def test_export_only_audited_snapshot_and_report(self):
        self.put(".env", "do not read")
        self.put("outputs/browser/private.md", "do not read")
        report, files = audit(self.root)
        self.put("README.md", "Changed after audit\n")
        archive = export(self.root, report, files)
        self.assertEqual(json.loads(archive.with_suffix(".json").read_text(encoding="utf-8")), report)
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(set(bundle.namelist()), {"Supportops/README.md", "RELEASE_REVIEW.json"})
            self.assertEqual(bundle.read("Supportops/README.md"), files["README.md"])
            self.assertFalse(json.loads(bundle.read("RELEASE_REVIEW.json"))["public_release_ready"])

    def test_export_rejects_findings_and_changed_payload(self):
        report, files = audit(self.root)
        files["README.md"] = b"changed"
        with self.assertRaises(ValueError):
            export(self.root, report, files)
        report, files = audit(self.root)
        report["candidate_scan_passed"] = False
        with self.assertRaises(ValueError):
            export(self.root, report, files)

    def test_non_utf8_fails_closed(self):
        self.root.joinpath("main.py").write_bytes(b"\xff")
        report, _ = audit(self.root)
        self.assertFalse(report["candidate_scan_passed"])
        self.assertEqual(report["findings"][0]["rule"], "non_utf8")

    def test_symlink_source_is_not_read_and_fails_closed(self):
        outside = self.put("private.txt", "do not read")
        link = self.root / "docs"
        try:
            link.symlink_to(outside.parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink privilege unavailable")
        report, files = audit(self.root)
        self.assertFalse(report["candidate_scan_passed"])
        self.assertEqual(set(files), {"README.md"})

    def test_index_is_preserved_and_excluded_blobs_not_opened(self):
        self.init_git()
        self.put(".idea/workspace.xml", "private index blob")
        self.put(".env.supportops", "private index blob")
        self.git("add", "--", "README.md", ".idea/workspace.xml", ".env.supportops")
        before = (self.root / ".git/index").read_bytes()
        from scripts import check_release
        original = check_release._git
        def guarded(root, *args):
            if args[:2] == ("cat-file", "blob"):
                self.assertEqual(original(root, *args), b"# Public demo\n")
            return original(root, *args)
        with patch.object(check_release, "_git", guarded):
            report, _ = audit(self.root)
        self.assertEqual(before, (self.root / ".git/index").read_bytes())
        self.assertEqual(report["excluded_index_paths"], [".env.supportops", ".idea/workspace.xml"])
        self.assertFalse(report["git_index_ready"])

    def test_old_index_secret_is_detected_even_if_worktree_fixed(self):
        self.init_git()
        secret = "sk-" + "abcd0123" * 6
        self.put("main.py", 'value = "' + secret + '"\n')
        self.git("add", "--", "README.md", "main.py")
        self.put("main.py", "# fixed working tree\n")
        report, _ = audit(self.root)
        self.assertTrue(report["candidate_scan_passed"])
        self.assertFalse(report["git_index_ready"])
        self.assertIn("main.py", report["stale_index_paths"])
        self.assertEqual(report["findings"][0]["origin"], "index")
        self.assertNotIn(secret, json.dumps(report))

    def test_git_crlf_normalization_is_not_stale_source(self):
        self.init_git()
        self.git("config", "core.autocrlf", "true")
        self.root.joinpath("main.py").write_bytes(b"# public\r\n")
        self.git("add", "--", "README.md", "main.py")
        report, _ = audit(self.root)
        self.assertTrue(report["git_index_ready"])
        self.assertIn("main.py", report["line_ending_normalized_paths"])
        self.root.joinpath("main.py").write_bytes(b"# actual edit\r\n")
        report, _ = audit(self.root)
        self.assertFalse(report["git_index_ready"])
        self.assertIn("main.py", report["stale_index_paths"])


if __name__ == "__main__":
    unittest.main()
