import unittest
from unittest.mock import patch

from news_tools import (
    NewsToolError,
    deduplicate_news_items,
    search_ai_news,
)


class TestNewsTools(unittest.TestCase):
    @patch("news_tools.GoogleSearch")
    def test_search_ai_news_returns_normalized_items(self, mock_google_search):
        mock_google_search.return_value.get_dict.return_value = {
            "news_results": [
                {
                    "title": "某个 AI 新闻标题",
                    "link": "https://example.com/ai-news",
                    "snippet": "这是新闻摘要。",
                    "source": "示例媒体",
                    "date": "3 小时前",
                },
                {
                    "title": "缺少链接的无效新闻",
                    "snippet": "该条目应被跳过。",
                },
            ]
        }

        items = search_ai_news(
            query="AI 新闻",
            num_results=5,
            api_key="fake-key-for-test",
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "某个 AI 新闻标题")
        self.assertEqual(items[0]["url"], "https://example.com/ai-news")
        self.assertEqual(items[0]["source"], "示例媒体")
        self.assertEqual(items[0]["category"], "news")

    def test_search_ai_news_rejects_empty_query(self):
        with self.assertRaises(NewsToolError):
            search_ai_news(
                query="   ",
                api_key="fake-key-for-test",
            )

    @patch("news_tools.GoogleSearch")
    def test_search_ai_news_redacts_api_key_from_request_error(self, mock_google_search):
        mock_google_search.return_value.get_dict.side_effect = RuntimeError(
            "请求失败：https://example.com/search?api_key=secret-value&output=json"
        )

        with self.assertRaises(NewsToolError) as context:
            search_ai_news(api_key="fake-key-for-test")

        self.assertNotIn("secret-value", str(context.exception))
        self.assertIn("api_key=***", str(context.exception))

    def test_deduplicate_news_items_by_url(self):
        items = [
            {
                "title": "新闻 A",
                "url": "https://example.com/a",
                "snippet": "摘要 A",
                "source": "示例来源",
                "published_at": "1 小时前",
                "category": "news",
            },
            {
                "title": "重复的新闻 A",
                "url": "https://example.com/a/",
                "snippet": "另一份摘要",
                "source": "另一个来源",
                "published_at": "2 小时前",
                "category": "news",
            },
        ]

        unique_items = deduplicate_news_items(items)

        self.assertEqual(len(unique_items), 1)
        self.assertEqual(unique_items[0]["title"], "新闻 A")


if __name__ == "__main__":
    unittest.main()
