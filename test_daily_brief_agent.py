import unittest

from daily_brief_agent import DailyBriefAgent
from prompts import DAILY_BRIEF_SYSTEM_PROMPT


class FakeLLMClient:
    """替代真实 DeepSeek 客户端，记录输入并返回固定 Markdown。"""

    def __init__(self) -> None:
        self.prompt = ""
        self.system_prompt = ""

    def generate(self, prompt: str, system_prompt: str) -> str:
        self.prompt = prompt
        self.system_prompt = system_prompt
        return "# AI 每日简报｜测试日期\n\n## 今日要点\n\n- 测试日报。"


class FakeSearchNews:
    """替代 SerpApi 搜索函数，返回可控的固定新闻资料。"""

    def __init__(self) -> None:
        self.called = False

    def __call__(self) -> list[dict[str, str]]:
        self.called = True
        return [
            {
                "title": "AI 新闻 A",
                "url": "https://example.com/ai-a",
                "snippet": "新闻 A 的摘要。",
                "source": "示例媒体",
                "published_at": "1 小时前",
                "category": "news",
            },
            {
                "title": "AI 新闻 A 的重复结果",
                "url": "https://example.com/ai-a/",
                "snippet": "重复摘要。",
                "source": "另一家媒体",
                "published_at": "2 小时前",
                "category": "news",
            },
        ]


class TestDailyBriefAgent(unittest.TestCase):
    def test_generate_daily_brief_uses_injected_dependencies(self):
        fake_llm = FakeLLMClient()
        fake_search = FakeSearchNews()
        agent = DailyBriefAgent(
            llm_client=fake_llm,
            search_news_func=fake_search,
        )

        brief = agent.generate_daily_brief()

        self.assertTrue(fake_search.called)
        self.assertEqual(brief, "# AI 每日简报｜测试日期\n\n## 今日要点\n\n- 测试日报。")
        self.assertEqual(fake_llm.system_prompt, DAILY_BRIEF_SYSTEM_PROMPT)
        self.assertIn("AI 新闻 A", fake_llm.prompt)
        self.assertNotIn("AI 新闻 A 的重复结果", fake_llm.prompt)
        self.assertIn('"source": "示例媒体"', fake_llm.prompt)

    def test_generate_daily_brief_includes_papers_and_repositories(self):
        fake_llm = FakeLLMClient()
        agent = DailyBriefAgent(
            llm_client=fake_llm,
            search_news_func=lambda: [],
            search_papers_func=lambda: [
                {
                    "title": "论文 A",
                    "url": "https://arxiv.org/abs/1",
                    "summary": "论文摘要",
                    "authors": "作者",
                    "published_at": "今天",
                    "category": "paper",
                }
            ],
            search_repositories_func=lambda: [
                {
                    "title": "owner/project",
                    "url": "https://github.com/owner/project",
                    "summary": "项目简介",
                    "stars": 10,
                    "language": "Python",
                    "pushed_at": "今天",
                    "is_fork": False,
                    "is_archived": False,
                    "category": "repository",
                }
            ],
        )

        agent.generate_daily_brief()

        self.assertIn('"papers": [', fake_llm.prompt)
        self.assertIn("论文 A", fake_llm.prompt)
        self.assertIn('"repositories": [', fake_llm.prompt)
        self.assertIn("owner/project", fake_llm.prompt)


if __name__ == "__main__":
    unittest.main()
