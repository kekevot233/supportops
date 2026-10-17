"""无需 shell 的只读文件系统探索工具。"""

from __future__ import annotations

import fnmatch
import json
import shlex
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ...core.exceptions import ToolError
from ..base import Tool, ToolParameter


class TerminalTool(Tool):
    """在固定工作区内提供有界、只读、无 shell 的即时文件访问。

    支持命令：``pwd``、``cd [path]``、``ls [path]``、
    ``tree [path] [depth]``、``cat file``、``head file [lines]``、
    ``tail file [lines]``、``find [path] [glob]``、
    ``grep text [path] [glob]``、``wc file`` 与 ``stat path``。
    """

    _ALLOWED_COMMANDS = {
        "pwd",
        "cd",
        "ls",
        "tree",
        "cat",
        "head",
        "tail",
        "find",
        "grep",
        "wc",
        "stat",
    }
    _SHELL_MARKERS = ("|", "&", ";", ">", "<", "`", "\r", "\n", "\x00")
    _SENSITIVE_NAMES = {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
    }
    _SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}

    def __init__(
        self,
        workspace: str | Path,
        *,
        max_output_chars: int = 100_000,
        max_file_size: int = 2 * 1024 * 1024,
        max_entries: int = 1000,
        max_depth: int = 5,
        max_matches: int = 200,
    ) -> None:
        super().__init__(
            name="terminal",
            description=(
                "在受控工作区中即时探索文件。只支持只读命令，不启动 shell，"
                "不支持管道、重定向或任意程序执行。"
            ),
        )
        self.max_output_chars = self._positive_int(
            max_output_chars,
            "max_output_chars",
            minimum=128,
        )
        self.max_file_size = self._positive_int(
            max_file_size,
            "max_file_size",
            minimum=128,
        )
        self.max_entries = self._positive_int(max_entries, "max_entries")
        self.max_depth = self._positive_int(max_depth, "max_depth")
        self.max_matches = self._positive_int(max_matches, "max_matches")
        try:
            resolved = Path(workspace).expanduser().resolve(strict=True)
        except (OSError, TypeError, ValueError) as error:
            raise ToolError("终端工作区不存在或无法访问。") from error
        if not resolved.is_dir():
            raise ToolError("终端工作区必须是目录。")
        self.workspace = resolved
        self.current_dir = resolved
        self._lock = threading.RLock()

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="command",
                type="string",
                description=(
                    "只读命令。支持 pwd/cd/ls/tree/cat/head/tail/find/grep/wc/stat；"
                    "路径含空格时使用引号。"
                ),
            )
        ]

    def _run(self, parameters: dict[str, Any]) -> str:
        command = parameters["command"]
        tokens = self._parse(command)
        name, arguments = tokens[0].lower(), tokens[1:]
        if name not in self._ALLOWED_COMMANDS:
            raise ToolError(f"不允许的只读命令：{name}。")
        with self._lock:
            handlers = {
                "pwd": self._pwd,
                "cd": self._cd,
                "ls": self._ls,
                "tree": self._tree,
                "cat": self._cat,
                "head": self._head,
                "tail": self._tail,
                "find": self._find,
                "grep": self._grep,
                "wc": self._wc,
                "stat": self._stat,
            }
            output = handlers[name](arguments)
        return self._limit_output(output or "(无结果)")

    def _pwd(self, arguments: list[str]) -> str:
        self._require_arity("pwd", arguments, 0, 0)
        return self._display_path(self.current_dir)

    def _cd(self, arguments: list[str]) -> str:
        self._require_arity("cd", arguments, 0, 1)
        target = self._resolve(arguments[0] if arguments else ".", directory=True)
        self.current_dir = target
        return f"当前目录：{self._display_path(target)}"

    def _ls(self, arguments: list[str]) -> str:
        self._require_arity("ls", arguments, 0, 1)
        target = self._resolve(arguments[0] if arguments else ".")
        if target.is_file():
            return self._format_entry(target)
        entries = self._children(target)
        if len(entries) > self.max_entries:
            entries = entries[: self.max_entries]
            suffix = f"\n[…条目已截断，最多 {self.max_entries} 项…]"
        else:
            suffix = ""
        return "\n".join(self._format_entry(path) for path in entries) + suffix

    def _tree(self, arguments: list[str]) -> str:
        self._require_arity("tree", arguments, 0, 2)
        target = self._resolve(arguments[0] if arguments else ".", directory=True)
        depth = self.max_depth
        if len(arguments) == 2:
            depth = self._bounded_int(arguments[1], "tree depth", 1, self.max_depth)
        lines = [self._display_path(target)]
        count = 0
        truncated = False

        def visit(directory: Path, level: int) -> None:
            nonlocal count, truncated
            if level > depth or truncated:
                return
            children = self._children(directory)
            for child in children:
                if count >= self.max_entries:
                    truncated = True
                    return
                count += 1
                marker = "/" if child.is_dir() and not child.is_symlink() else ""
                lines.append(f"{'  ' * level}- {child.name}{marker}")
                if child.is_dir() and not child.is_symlink():
                    visit(child, level + 1)

        visit(target, 1)
        if truncated:
            lines.append(f"[…目录树已截断，最多 {self.max_entries} 项…]")
        return "\n".join(lines)

    def _cat(self, arguments: list[str]) -> str:
        self._require_arity("cat", arguments, 1, 1)
        return self._read_text(self._resolve(arguments[0], file=True))

    def _head(self, arguments: list[str]) -> str:
        self._require_arity("head", arguments, 1, 2)
        count = self._line_count(arguments[1]) if len(arguments) == 2 else 20
        lines = self._read_text(self._resolve(arguments[0], file=True)).splitlines()
        return "\n".join(lines[:count]) or "(空文件)"

    def _tail(self, arguments: list[str]) -> str:
        self._require_arity("tail", arguments, 1, 2)
        count = self._line_count(arguments[1]) if len(arguments) == 2 else 20
        lines = self._read_text(self._resolve(arguments[0], file=True)).splitlines()
        return "\n".join(lines[-count:]) or "(空文件)"

    def _find(self, arguments: list[str]) -> str:
        self._require_arity("find", arguments, 0, 2)
        root = self._resolve(arguments[0] if arguments else ".", directory=True)
        pattern = arguments[1] if len(arguments) == 2 else "*"
        self._validate_glob(pattern)
        matches = []
        for path in self._walk(root):
            if fnmatch.fnmatch(path.name, pattern):
                matches.append(self._display_path(path))
                if len(matches) >= self.max_matches:
                    matches.append(f"[…结果已截断，最多 {self.max_matches} 项…]")
                    break
        return "\n".join(matches) or "(无匹配文件)"

    def _grep(self, arguments: list[str]) -> str:
        self._require_arity("grep", arguments, 1, 3)
        pattern = arguments[0]
        if not pattern or len(pattern) > 500:
            raise ToolError("grep 搜索文本必须为 1 到 500 个字符。")
        target = self._resolve(arguments[1] if len(arguments) >= 2 else ".")
        glob_pattern = arguments[2] if len(arguments) == 3 else "*"
        self._validate_glob(glob_pattern)
        files = [target] if target.is_file() else list(self._walk(target, files_only=True))
        matches = []
        for path in files:
            if not fnmatch.fnmatch(path.name, glob_pattern):
                continue
            try:
                text = self._read_text(path)
            except ToolError:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    excerpt = line[:500]
                    matches.append(
                        f"{self._display_path(path)}:{line_number}:{excerpt}"
                    )
                    if len(matches) >= self.max_matches:
                        matches.append(f"[…结果已截断，最多 {self.max_matches} 项…]")
                        return "\n".join(matches)
        return "\n".join(matches) or "(无匹配内容)"

    def _wc(self, arguments: list[str]) -> str:
        self._require_arity("wc", arguments, 1, 1)
        text = self._read_text(self._resolve(arguments[0], file=True))
        payload = {
            "lines": len(text.splitlines()),
            "words": len(text.split()),
            "characters": len(text),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _stat(self, arguments: list[str]) -> str:
        self._require_arity("stat", arguments, 1, 1)
        path = self._resolve(arguments[0])
        stat = path.stat()
        payload = {
            "path": self._display_path(path),
            "type": "directory" if path.is_dir() else "file",
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime,
                tz=timezone.utc,
            ).isoformat(),
            "symlink": path.is_symlink(),
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _walk(self, root: Path, *, files_only: bool = False) -> Iterator[Path]:
        stack: list[tuple[Path, int]] = [(root, 0)]
        visited = 0
        while stack and visited < self.max_entries:
            directory, depth = stack.pop()
            if depth >= self.max_depth:
                continue
            children = self._children(directory)
            for child in reversed(children):
                if visited >= self.max_entries:
                    break
                visited += 1
                if not files_only or child.is_file():
                    yield child
                if child.is_dir() and not child.is_symlink():
                    stack.append((child, depth + 1))

    def _children(self, directory: Path) -> list[Path]:
        try:
            result = [path for path in directory.iterdir() if not self._is_sensitive(path)]
        except OSError as error:
            raise ToolError("目录无法读取。") from error
        result.sort(key=lambda path: (not path.is_dir(), path.name.lower(), path.name))
        return result

    def _read_text(self, path: Path) -> str:
        if self._is_sensitive(path):
            raise ToolError("不允许读取敏感文件。")
        try:
            size = path.stat().st_size
            if size > self.max_file_size:
                raise ToolError("文件超过允许读取的大小上限。")
            data = path.read_bytes()
        except ToolError:
            raise
        except OSError as error:
            raise ToolError("文件无法读取。") from error
        if b"\x00" in data:
            raise ToolError("不支持读取二进制文件。")
        try:
            return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError as error:
            raise ToolError("文件不是有效的 UTF-8 文本。") from error

    def _resolve(
        self,
        raw_path: str,
        *,
        file: bool = False,
        directory: bool = False,
    ) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ToolError("路径不能为空。")
        requested = Path(raw_path.strip())
        candidate = requested if requested.is_absolute() else self.current_dir / requested
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self.workspace)
        except (OSError, ValueError) as error:
            raise ToolError("路径不存在或超出终端工作区。") from error
        if self._is_sensitive(resolved):
            raise ToolError("不允许访问敏感文件。")
        if file and not resolved.is_file():
            raise ToolError("目标必须是普通文件。")
        if directory and not resolved.is_dir():
            raise ToolError("目标必须是目录。")
        return resolved

    def _display_path(self, path: Path) -> str:
        if path == self.workspace:
            return "."
        return path.relative_to(self.workspace).as_posix()

    def _format_entry(self, path: Path) -> str:
        if path.is_dir():
            kind, size = "dir", "-"
        elif path.is_symlink():
            kind, size = "link", "-"
        else:
            kind, size = "file", str(path.stat().st_size)
        return f"{kind}\t{size}\t{path.name}"

    def _is_sensitive(self, path: Path) -> bool:
        try:
            relative = path.resolve(strict=False).relative_to(self.workspace)
        except ValueError:
            return True
        for part in relative.parts:
            lowered = part.lower()
            if lowered in self._SENSITIVE_NAMES or lowered.startswith(".env."):
                return True
            if Path(lowered).suffix in self._SENSITIVE_SUFFIXES:
                return True
        return False

    def _parse(self, command: str) -> list[str]:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("终端命令不能为空。")
        if any(marker in command for marker in self._SHELL_MARKERS) or "$(" in command:
            raise ToolError("不支持 shell 运算符、管道、重定向或命令替换。")
        try:
            raw_tokens = shlex.split(command, posix=False)
        except ValueError as error:
            raise ToolError("终端命令引号不匹配。") from error
        tokens = []
        for token in raw_tokens:
            if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
                token = token[1:-1]
            tokens.append(token)
        if not tokens:
            raise ToolError("终端命令不能为空。")
        return tokens

    def _limit_output(self, output: str) -> str:
        if len(output) <= self.max_output_chars:
            return output
        marker = "\n[…输出已截断…]"
        return output[: self.max_output_chars - len(marker)] + marker

    @staticmethod
    def _require_arity(name: str, arguments: list[str], minimum: int, maximum: int) -> None:
        if not minimum <= len(arguments) <= maximum:
            raise ToolError(
                f"命令 '{name}' 参数数量无效，需要 {minimum} 到 {maximum} 个参数。"
            )

    @staticmethod
    def _positive_int(value: int, name: str, *, minimum: int = 1) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ToolError(f"{name} 必须是至少 {minimum} 的整数。")
        return value

    @staticmethod
    def _bounded_int(value: str, name: str, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as error:
            raise ToolError(f"{name} 必须是整数。") from error
        if not minimum <= number <= maximum:
            raise ToolError(f"{name} 必须在 {minimum} 到 {maximum} 之间。")
        return number

    @classmethod
    def _line_count(cls, value: str) -> int:
        return cls._bounded_int(value, "行数", 1, 10_000)

    @staticmethod
    def _validate_glob(pattern: str) -> None:
        if (
            not isinstance(pattern, str)
            or not pattern
            or len(pattern) > 200
            or ".." in pattern
            or "/" in pattern
            or "\\" in pattern
        ):
            raise ToolError("文件匹配模式无效；只允许文件名 glob。")


__all__ = ["TerminalTool"]
