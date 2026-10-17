import unittest
from datetime import date
from unittest.mock import Mock, patch

import requests

from research_tools import (
    ResearchToolError,
    deduplicate_items_by_url,
    search_arxiv_papers,
    search_featured_agent_repositories,
    search_github_repositories,
)


ARXIV_RESPONSE = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>  A Paper\n About AI  </title>
    <id>https://arxiv.org/abs/1234.5678</id>
    <summary>  First line.\n Second line. </summary>
    <published>2026-09-12T00:00:00Z</published>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
  </entry>
</feed>'''


class TestResearchTools(unittest.TestCase):
    @patch("research_tools.requests.get")
    def test_search_arxiv_papers_returns_normalized_items(self, mock_get):
        mock_get.return_value.content = ARXIV_RESPONSE

        papers = search_arxiv_papers(query="all:AI", max_results=3)

        self.assertEqual(mock_get.call_args.kwargs["params"]["sortBy"], "submittedDate")
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["title"], "A Paper About AI")
        self.assertEqual(papers[0]["authors"], "Alice, Bob")
        self.assertEqual(papers[0]["category"], "paper")

    @patch("research_tools.time.sleep")
    @patch("research_tools.requests.get")
    def test_search_arxiv_papers_retries_timeout_then_succeeds(
        self,
        mock_get,
        mock_sleep,
    ):
        successful_response = Mock()
        successful_response.content = ARXIV_RESPONSE
        mock_get.side_effect = [
            requests.ReadTimeout("Fake 首次超时"),
            successful_response,
        ]

        papers = search_arxiv_papers(query="all:AI", max_results=1)

        self.assertEqual(len(papers), 1)
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(3.0)

    @patch("research_tools.time.sleep")
    @patch("research_tools.requests.get")
    def test_search_arxiv_papers_stops_after_two_timeouts(
        self,
        mock_get,
        mock_sleep,
    ):
        mock_get.side_effect = requests.ReadTimeout("Fake 持续超时")

        with self.assertRaisesRegex(ResearchToolError, "已尝试 2 次"):
            search_arxiv_papers(query="all:AI", max_results=1)

        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(3.0)

    @patch("research_tools.time.sleep")
    @patch("research_tools.requests.get")
    def test_search_arxiv_papers_retries_rate_limit_then_succeeds(
        self,
        mock_get,
        mock_sleep,
    ):
        rate_limited_response = Mock()
        rate_limited_response.status_code = 429
        rate_limited_response.headers = {"Retry-After": "5"}
        rate_limited_response.raise_for_status.side_effect = requests.HTTPError(
            "Fake 429",
            response=rate_limited_response,
        )
        successful_response = Mock()
        successful_response.content = ARXIV_RESPONSE
        mock_get.side_effect = [rate_limited_response, successful_response]

        papers = search_arxiv_papers(query="all:AI", max_results=1)

        self.assertEqual(len(papers), 1)
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(5.0)

    @patch("research_tools.requests.get")
    def test_search_github_repositories_returns_normalized_items(self, mock_get):
        mock_get.return_value.json.return_value = {
            "items": [
                {
                    "full_name": "example/ai-project",
                    "html_url": "https://github.com/example/ai-project",
                    "description": " An AI\n project. ",
                    "stargazers_count": 42,
                    "language": "Python",
                    "pushed_at": "2026-09-12T00:00:00Z",
                    "fork": False,
                    "archived": False,
                }
            ]
        }

        repositories = search_github_repositories(query="AI", max_results=3)

        self.assertEqual(mock_get.call_args.kwargs["params"]["sort"], "updated")
        self.assertNotIn("Authorization", mock_get.call_args.kwargs["headers"])
        self.assertEqual(repositories[0]["stars"], 42)
        self.assertEqual(repositories[0]["summary"], "An AI project.")
        self.assertEqual(repositories[0]["pushed_at"], "2026-09-12T00:00:00Z")
        self.assertEqual(repositories[0]["category"], "repository")

    @patch("research_tools.requests.get")
    def test_search_featured_agent_repositories_applies_all_filters(self, mock_get):
        recent_date = "2026-09-10T12:00:00Z"
        mock_get.return_value.json.return_value = {
            "items": [
                {
                    "full_name": "example/high-star-agent",
                    "html_url": "https://github.com/example/high-star-agent",
                    "description": "AI agent project",
                    "stargazers_count": 2000,
                    "language": "Python",
                    "pushed_at": recent_date,
                    "fork": False,
                    "archived": False,
                },
                {
                    "full_name": "example/eligible-agent",
                    "html_url": "https://github.com/example/eligible-agent",
                    "description": "LLM agent project",
                    "stargazers_count": 600,
                    "language": "Python",
                    "pushed_at": recent_date,
                    "fork": False,
                    "archived": False,
                },
                {
                    "full_name": "example/old-agent",
                    "html_url": "https://github.com/example/old-agent",
                    "description": "old project",
                    "stargazers_count": 5000,
                    "language": "Python",
                    "pushed_at": "2026-07-01T12:00:00Z",
                    "fork": False,
                    "archived": False,
                },
                {
                    "full_name": "example/fork-agent",
                    "html_url": "https://github.com/example/fork-agent",
                    "description": "fork project",
                    "stargazers_count": 3000,
                    "language": "Python",
                    "pushed_at": recent_date,
                    "fork": True,
                    "archived": False,
                },
            ]
        }

        repositories = search_featured_agent_repositories(
            max_results=5,
            min_stars=500,
            active_days=30,
            today=date(2026, 9, 12),
        )

        self.assertEqual(
            [item["title"] for item in repositories],
            ["example/high-star-agent", "example/eligible-agent"],
        )
        self.assertEqual(mock_get.call_count, 3)
        for call in mock_get.call_args_list:
            params = call.kwargs["params"]
            self.assertEqual(params["sort"], "stars")
            self.assertIn("stars:>=500", params["q"])
            self.assertIn("archived:false", params["q"])
            self.assertIn("fork:false", params["q"])

    def test_research_tools_reject_empty_query(self):
        with self.assertRaises(ResearchToolError):
            search_arxiv_papers(query="  ")

        with self.assertRaises(ResearchToolError):
            search_github_repositories(query="  ")

    def test_deduplicate_items_by_url(self):
        items = [
            {"url": "https://example.com/item", "title": "first"},
            {"url": "https://example.com/item/", "title": "duplicate"},
        ]

        unique_items = deduplicate_items_by_url(items)

        self.assertEqual(unique_items, [items[0]])


if __name__ == "__main__":
    unittest.main()
