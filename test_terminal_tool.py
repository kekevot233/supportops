import json
import tempfile
import unittest
from pathlib import Path

from hello_agents import TerminalTool, ToolError, ToolRegistry


class TestTerminalTool(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name) / "project"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "main.py").write_text(
            "def main():\n    return 'hello'\n\n# TODO: test\n",
            encoding="utf-8",
        )
        (self.workspace / "README.md").write_text(
            "Hello Agent\nSecond line\nThird line\n",
            encoding="utf-8",
        )
        (self.workspace / "notes.txt").write_text(
            "alpha beta\nbeta gamma\n",
            encoding="utf-8",
        )
        (self.workspace / ".env").write_text(
            "SECRET=fake-sensitive-value",
            encoding="utf-8",
        )
        self.tool = TerminalTool(
            self.workspace,
            max_output_chars=2000,
            max_entries=100,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def execute(self, command):
        return self.tool.run({"command": command})

    def test_pwd_ls_and_cd_keep_state_inside_workspace(self):
        self.assertEqual(self.execute("pwd"), ".")
        listing = self.execute("ls")
        self.assertIn("README.md", listing)
        self.assertIn("src", listing)
        self.assertNotIn(".env", listing)

        self.assertEqual(self.execute("cd src"), "当前目录：src")
        self.assertEqual(self.execute("pwd"), "src")
        self.assertIn("main.py", self.execute("ls"))
        self.assertEqual(self.execute("cd .."), "当前目录：.")

    def test_cat_head_tail_and_wc_are_read_only(self):
        self.assertIn("Hello Agent", self.execute("cat README.md"))
        self.assertEqual(self.execute("head README.md 1"), "Hello Agent")
        self.assertEqual(self.execute("tail README.md 1"), "Third line")
        counts = json.loads(self.execute("wc notes.txt"))
        self.assertEqual(counts, {"lines": 2, "words": 4, "characters": 22})

    def test_find_grep_tree_and_stat_are_bounded(self):
        found = self.execute("find . *.py")
        self.assertEqual(found, "src/main.py")
        matched = self.execute('grep "TODO" . *.py')
        self.assertIn("src/main.py:4:# TODO: test", matched)
        tree = self.execute("tree . 2")
        self.assertIn("- src/", tree)
        self.assertIn("- main.py", tree)
        stat = json.loads(self.execute("stat README.md"))
        self.assertEqual(stat["path"], "README.md")
        self.assertEqual(stat["type"], "file")

    def test_rejects_shell_features_and_non_whitelisted_commands(self):
        invalid = (
            "cat README.md | grep Hello",
            "cat README.md > copy.txt",
            "pwd; whoami",
            "echo $(whoami)",
            "rm README.md",
            "python script.py",
        )
        for command in invalid:
            with self.subTest(command=command):
                with self.assertRaises(ToolError):
                    self.execute(command)

    def test_rejects_path_escape_and_sensitive_files(self):
        outside = self.workspace.parent / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        for command in ("cat ../outside.txt", "cd ..", "cat .env", "stat .env"):
            with self.subTest(command=command):
                with self.assertRaises(ToolError):
                    self.execute(command)

    def test_rejects_binary_and_oversized_files(self):
        (self.workspace / "binary.bin").write_bytes(b"abc\x00def")
        small_limit_tool = TerminalTool(self.workspace, max_file_size=128)
        (self.workspace / "large.txt").write_text("x" * 129, encoding="utf-8")

        with self.assertRaises(ToolError):
            self.execute("cat binary.bin")
        with self.assertRaises(ToolError):
            small_limit_tool.run({"command": "cat large.txt"})

    def test_output_is_truncated_at_configured_boundary(self):
        bounded = TerminalTool(self.workspace, max_output_chars=128)
        (self.workspace / "long.txt").write_text("内容" * 200, encoding="utf-8")

        output = bounded.run({"command": "cat long.txt"})

        self.assertLessEqual(len(output), 128)
        self.assertTrue(output.endswith("[…输出已截断…]"))

    def test_command_validation_and_standard_registry_integration(self):
        with self.assertRaises(ToolError):
            self.execute("head README.md 0")
        with self.assertRaises(ToolError):
            self.execute("find . ../*.py")
        with self.assertRaises(ToolError):
            self.execute('cat "unterminated')

        registry = ToolRegistry()
        registry.register_tool(self.tool)
        self.assertEqual(registry.execute_tool("terminal", {"command": "pwd"}), ".")
        schema = self.tool.to_openai_schema()
        self.assertEqual(schema["function"]["name"], "terminal")


if __name__ == "__main__":
    unittest.main()
