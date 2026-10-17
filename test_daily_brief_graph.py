import tempfile
import unittest
from datetime import date
from pathlib import Path

from daily_brief_graph import DailyBriefGraph
from email_notifier import EmailNotificationError


TEST_DATE = date(2026, 9, 13)
NEWS_URL = "https://example.com/news"
PAPER_URL = "https://arxiv.org/abs/2609.00001"
REPOSITORY_URL = "https://github.com/example/agent"


def make_valid_brief() -> str:
    return f"""# AI 每日简报｜{TEST_DATE.isoformat()}

## AI 技术新闻

- [新闻]({NEWS_URL})

## AI 学术论文

- [论文]({PAPER_URL})

## AI 开源项目

- [项目]({REPOSITORY_URL})
"""


class FakeLLMClient:
    """按顺序返回预设结果，不调用 DeepSeek。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def generate(self, prompt: str, system_prompt: str) -> str:
        del system_prompt
        self.prompts.append(prompt)
        return self.responses[len(self.prompts) - 1]


class FakeEmailSender:
    """记录发送路径，不连接 SMTP。"""

    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.paths: list[Path] = []

    def __call__(self, brief_path: Path) -> None:
        self.paths.append(brief_path)
        if self.should_fail:
            raise EmailNotificationError("Fake SMTP 故障")


def fake_news_search() -> list[dict]:
    return [
        {
            "title": "新闻",
            "url": NEWS_URL,
            "snippet": "摘要",
            "source": "测试媒体",
            "published_at": "今天",
            "category": "news",
        }
    ]


def fake_paper_search() -> list[dict]:
    return [
        {
            "title": "论文",
            "url": PAPER_URL,
            "summary": "摘要",
            "authors": "测试作者",
            "published_at": "今天",
            "category": "paper",
        }
    ]


def fake_repository_search() -> list[dict]:
    return [
        {
            "title": "example/agent",
            "url": REPOSITORY_URL,
            "summary": "简介",
            "stars": 1000,
            "language": "Python",
            "pushed_at": "今天",
            "is_fork": False,
            "is_archived": False,
            "category": "repository",
        }
    ]


class TestDailyBriefGraph(unittest.TestCase):
    def build_graph(
        self,
        output_dir: Path,
        llm: FakeLLMClient,
        email_sender: FakeEmailSender,
    ) -> DailyBriefGraph:
        return DailyBriefGraph(
            llm_client=llm,
            search_news_func=fake_news_search,
            search_papers_func=fake_paper_search,
            search_repositories_func=fake_repository_search,
            send_email_func=email_sender,
            output_dir=output_dir,
            date_provider=lambda: TEST_DATE,
        )

    def test_succeeds_on_first_generation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            llm = FakeLLMClient([make_valid_brief()])
            email_sender = FakeEmailSender()
            graph = self.build_graph(Path(temporary_directory), llm, email_sender)

            result = graph.run(thread_id="first-success")

            output_path = Path(result["output_path"])
            self.assertTrue(result["validation_passed"])
            self.assertEqual(result["retry_count"], 1)
            self.assertTrue(result["email_sent"])
            self.assertTrue(output_path.is_file())
            self.assertEqual(email_sender.paths, [output_path])
            self.assertEqual(len(llm.prompts), 1)

            snapshot = graph.app.get_state(
                {"configurable": {"thread_id": "first-success"}}
            )
            self.assertEqual(snapshot.values["output_path"], str(output_path))
            self.assertEqual(snapshot.next, ())

    def test_retries_after_failure_then_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            llm = FakeLLMClient(["错误：Fake 模型失败", make_valid_brief()])
            email_sender = FakeEmailSender()
            graph = self.build_graph(Path(temporary_directory), llm, email_sender)

            result = graph.run(thread_id="retry-success")

            self.assertTrue(result["validation_passed"])
            self.assertEqual(result["retry_count"], 2)
            self.assertTrue(result["email_sent"])
            self.assertEqual(len(llm.prompts), 2)
            self.assertIn("上一版没有通过质量检查", llm.prompts[1])
            self.assertIn("以“错误：”开头", llm.prompts[1])
            self.assertIn("缺少栏目", llm.prompts[1])

    def test_stops_after_two_failed_generations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            llm = FakeLLMClient(["第一次不合格", "第二次仍不合格"])
            email_sender = FakeEmailSender()
            graph = self.build_graph(Path(temporary_directory), llm, email_sender)

            result = graph.run(thread_id="retry-stop")

            self.assertFalse(result["validation_passed"])
            self.assertEqual(result["retry_count"], 2)
            self.assertFalse(result["email_sent"])
            self.assertEqual(result["output_path"], "")
            self.assertIn("连续 2 次未通过质量检查", result["error"])
            self.assertEqual(len(llm.prompts), 2)
            self.assertEqual(email_sender.paths, [])
            self.assertEqual(list(Path(temporary_directory).iterdir()), [])

    def test_email_failure_keeps_saved_brief(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            llm = FakeLLMClient([make_valid_brief()])
            email_sender = FakeEmailSender(should_fail=True)
            graph = self.build_graph(Path(temporary_directory), llm, email_sender)

            result = graph.run(thread_id="email-failure")

            output_path = Path(result["output_path"])
            self.assertTrue(result["validation_passed"])
            self.assertTrue(output_path.is_file())
            self.assertFalse(result["email_sent"])
            self.assertIn("日报已保存，但邮件发送失败", result["error"])


if __name__ == "__main__":
    unittest.main()
