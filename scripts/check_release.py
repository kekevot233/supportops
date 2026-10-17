"""Read-only release inventory and heuristic scanning; never stages or publishes.

Only explicit source roots are traversed. Real env files, outputs and excluded
index entries are never opened. Reports contain locations/rules, not matches.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    ".gitignore", "README.md", "PROJECT_SUMMARY.md", "pyproject.toml",
    "CONTRIBUTING.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md",
    "requirements-ci.txt", "requirements-legacy-test.txt",
    "supportops.env.example", "email.env.example", "LICENSE", "LICENSE.txt",
    "install_daily_brief_task.ps1", "run_daily_brief_task.cmd",
}
TREES = {
    "hello_agents": {".py"}, "supportops": {".py", ".html", ".css", ".js"},
    "scripts": {".py", ".mjs"}, "docs": {".md"},
    "data/supportops": {".md", ".json"}, "benchmarks": {".json"},
    "configs/supportops": {".json"}, ".github/workflows": {".yml", ".yaml"},
}
ASSETS = {"docs/assets/supportops-documents.png"}
SKIP_DIRS = {".git", ".idea", ".venv", "__pycache__", "node_modules", "outputs", ".trash"}
MAX_BYTES = 3 * 1024 * 1024
APPROVED_LICENSE_SHA256 = "1349a4b6148492b44f629e64eed676612e234fe9a839e4f3b277c1482c8849f1"
OWNER_LICENSE_DECISION = b"Release license decision: CC-BY-NC-SA-4.0 (owner confirmed 2026-10-17)."
RULES = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "provider_token": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "personal_mail": re.compile(r"\b[A-Za-z0-9._%+-]+@(?:qq|163|gmail|outlook)\.com\b"),
}
# Reviewed public examples, not a blanket exemption for tests or env examples.
SAFE_MAIL = {"your_qq_number@qq.com"}
LITERAL_CREDENTIAL = re.compile(
    r'''(?i)["']?(?:api_key|llm_api_key|smtp_auth_code|password|access_token|client_secret)["']?\s*[:=]\s*["']([A-Za-z0-9_+=./-]{16,})["']''')
ENV_CREDENTIAL = re.compile(
    r"^\s*(?:API_KEY|LLM_API_KEY|SMTP_AUTH_CODE|PASSWORD|ACCESS_TOKEN|CLIENT_SECRET)\s*=\s*([A-Za-z0-9_+=./-]{16,})\s*$")
SAFE_LITERALS = {"replace_with_your_key", "your_qq_mail_authorization_code"}
# Reviewed negative-validation/MockTransport fixtures. Exempt only this exact
# value hash at this exact public source path; real test files are still scanned.
REVIEWED_FIXTURES = {
    "test_supportops_answer_evaluation.py": {"b4bdf7c3f7cd98c27b5b8a06a54a8e782365f6ea902496ef644e4f1fc82d8aa4"},
    "test_supportops_preflight.py": {"12f94b8b9f28a2b5c634385ba14e574bd0c67eaa41215a54418685639bdae891"},
    "test_supportops_provider_adapters.py": {"6fe074c801bc0a43370f565cb99008b7333ea99441623bdbdc23b0e4be470bfa"},
}


def allowed(relative: str) -> bool:
    """Allowlist check BEFORE stat/open, including for the Git index."""
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
        return False
    if any(p in SKIP_DIRS or p.startswith(".env") for p in path.parts):
        return False
    if path.name.lower() in {"credentials.json", "secrets.json", "tokens.json"}:
        return False
    if len(path.parts) == 1:
        return relative in ROOT_FILES or path.suffix == ".py"
    if relative in ASSETS:
        return True
    for tree, extensions in TREES.items():
        if relative.startswith(tree + "/") and path.suffix in extensions:
            return not tree.startswith("configs/") or path.name.endswith(".example.json")
    return False


def findings_for(relative: str, content: bytes, origin: str) -> list[dict]:
    if relative in ASSETS:
        return []  # This specific asset requires separate visual review.
    try:
        source = content.decode("utf-8-sig")
    except UnicodeError:
        return [{"path": relative, "origin": origin, "rule": "non_utf8"}]
    findings = []
    for number, line in enumerate(source.splitlines(), 1):
        for rule, pattern in RULES.items():
            if any(m.group() not in SAFE_MAIL for m in pattern.finditer(line)):
                findings.append({"path": relative, "origin": origin, "line": number, "rule": rule})
        for match in [*LITERAL_CREDENTIAL.finditer(line), *ENV_CREDENTIAL.finditer(line)]:
            value = match.group(1)
            entropy = -sum((value.count(c) / len(value)) * math.log2(value.count(c) / len(value))
                           for c in set(value))
            reviewed = hashlib.sha256(value.encode()).hexdigest() in REVIEWED_FIXTURES.get(relative, set())
            if value not in SAFE_LITERALS and not reviewed and entropy >= 3.5:
                findings.append({"path": relative, "origin": origin, "line": number,
                                 "rule": "credential_literal"})
    return findings


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    if result.returncode:
        raise ValueError("git_read_failed")
    return result.stdout


def collect(root: Path) -> tuple[dict[str, bytes], list[dict]]:
    root = root.resolve()
    files: dict[str, bytes] = {}
    issues: list[dict] = []
    paths = list(root.iterdir())
    for tree in TREES:
        start = root / tree
        # Reject symlinks/junctions in every ancestor before traversal.
        parents = [root.joinpath(*PurePosixPath(tree).parts[:i])
                   for i in range(1, len(PurePosixPath(tree).parts) + 1)]
        if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in parents):
            issues.append({"path": tree, "rule": "linked_tree"})
            continue
        if not start.is_dir():
            continue
        def walk_error(_error):
            issues.append({"path": tree, "rule": "walk_failed"})
        for directory, dirs, names in os.walk(start, followlinks=False, onerror=walk_error):
            kept = []
            for name in dirs:
                candidate = Path(directory) / name
                if name in SKIP_DIRS or name.startswith(".env"):
                    continue
                if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
                    issues.append({"path": candidate.relative_to(root).as_posix(), "rule": "linked_tree"})
                else:
                    kept.append(name)
            dirs[:] = kept
            paths.extend(Path(directory) / name for name in names)
    for path in sorted(set(paths)):
        relative = path.relative_to(root).as_posix()
        if not allowed(relative):
            continue
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("linked_file")
            if not path.is_file():
                continue
            if path.stat().st_size > MAX_BYTES:
                raise ValueError("file_too_large")
            content = path.read_bytes()
            if len(content) > MAX_BYTES:
                raise ValueError("file_too_large")
            files[relative] = content
        except (OSError, ValueError):
            issues.append({"path": relative, "rule": "unsafe_or_unreadable_file"})
    return files, issues


def audit(root: Path, *, check_index: bool = True) -> tuple[dict, dict[str, bytes]]:
    files, issues = collect(root)
    findings = [item for name, data in files.items() for item in findings_for(name, data, "working_tree")]
    excluded_index, stale_index, normalized_index, index_issues = [], [], [], []
    git_present = (root / ".git").exists()
    if check_index and git_present:
        try:
            config = subprocess.run(["git", "-C", str(root), "config", "--get", "core.autocrlf"],
                                    capture_output=True, check=False)
            normalize_crlf = config.stdout.strip().lower() in {b"true", b"input"}
            for entry in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
                if not entry:
                    continue
                meta, raw_path = entry.split(b"\t", 1)
                mode, object_id, stage = meta.decode("ascii").split()
                name = raw_path.decode("utf-8")
                if not allowed(name):
                    excluded_index.append(name)  # Never read excluded blobs.
                    continue
                if mode != "100644" and mode != "100755" or stage != "0":
                    index_issues.append({"path": name, "rule": "unsupported_index_entry"})
                    continue
                size = int(_git(root, "cat-file", "-s", object_id))
                if size > MAX_BYTES:
                    index_issues.append({"path": name, "rule": "index_blob_too_large"})
                    continue
                data = _git(root, "cat-file", "blob", object_id)
                findings.extend(findings_for(name, data, "index"))
                working = files.get(name)
                if data != working:
                    if normalize_crlf and working is not None and name not in ASSETS and data == working.replace(b"\r\n", b"\n"):
                        normalized_index.append(name)
                    else:
                        stale_index.append(name)
        except (OSError, ValueError, UnicodeError):
            index_issues.append({"rule": "index_audit_failed"})
    if not files or "README.md" not in files:
        issues.append({"rule": "missing_release_readme"})
    # Records this owner's explicit release decision, not a legal certification.
    # A random LICENSE file alone cannot clear this gate. Normalize Git newlines.
    license_confirmed = (
        hashlib.sha256(files.get("LICENSE", b"").replace(b"\r\n", b"\n")).hexdigest() == APPROVED_LICENSE_SHA256
        and OWNER_LICENSE_DECISION in files.get("THIRD_PARTY_NOTICES.md", b"")
    )
    candidate_clean = not issues and not any(f["origin"] == "working_tree" for f in findings)
    index_ready = bool(check_index and git_present and not
                       (excluded_index or stale_index or index_issues or findings or issues))
    report = {
        "repository": "kekevot233/supportops", "mode": "local_review_only",
        "candidate_scan_passed": candidate_clean,
        "public_release_ready": candidate_clean and index_ready and license_confirmed,
        "license_review": "owner_confirmed_CC_BY_NC_SA_4_0" if license_confirmed else "pending_source_scope_and_owner_decision",
        "git_index_checked": check_index and git_present,
        "git_index_ready": index_ready,
        "excluded_index_paths": sorted(excluded_index), "stale_index_paths": sorted(stale_index),
        "line_ending_normalized_paths": sorted(normalized_index),
        "index_issues": index_issues, "collection_issues": issues, "findings": findings,
        "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in sorted(files.items())],
        "limitations": ["Heuristic patterns are not a guarantee of absence of secrets.",
                        "Real env/credential files, excluded index blobs and Git history are not opened.",
                        "Only allowlisted public source files are considered; review the manifest manually.",
                        "PNG asset needs separate visual review; no OCR is performed.",
                        "License status records the explicit owner decision, not a legal compliance opinion.",
                        "Index is inspected only, never modified; this is not a commit or publication."],
    }
    return report, files


def export(root: Path, report: dict, files: dict[str, bytes]) -> Path:
    """Export the exact audited bytes, not a second possibly changed file read."""
    expected = [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in sorted(files.items())]
    if not report["candidate_scan_passed"] or expected != report["files"]:
        raise ValueError("candidate_not_clean_or_changed")
    target = root / "outputs" / "release"
    for parent in [root / "outputs", target]:
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("linked_output_directory")
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = target / f"Supportops-review-{stamp}.zip"
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in sorted(files.items()):
            bundle.writestr("Supportops/" + name, data)
        bundle.writestr("RELEASE_REVIEW.json", json.dumps(report, ensure_ascii=True, indent=2))
    with archive.with_suffix(".json").open("x", encoding="utf-8") as summary:
        json.dump(report, summary, ensure_ascii=True, indent=2)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", action="store_true", help="Create a local review-only ZIP under outputs/release")
    args = parser.parse_args()
    try:
        report, files = audit(ROOT)
        if args.export:
            report["archive"] = str(export(ROOT, report, files))
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0 if report["candidate_scan_passed"] and not report["index_issues"] and not report["findings"] else 1
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile):
        print('{"error":"RELEASE_CHECK_FAILED"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
