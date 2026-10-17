"""将 AI 新闻资料整理为 Markdown 每日简报的 Agent。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Protocol

from news_tools import NewsItem, deduplicate_news_items
from prompts import DAILY_BRIEF_SYSTEM_PROMPT
from research_tools import (
    PaperItem,
    RepositoryItem,
    deduplicate_items_by_url,
)


class LLMClient(Protocol):
    """日报 Agent 实际需要的最小 LLM 能力。"""

    def generate(self, prompt: str, system_prompt: str) -> str:
        """根据提示词生成文本。"""


SearchNewsFunction = Callable[[], list[NewsItem]]
SearchPapersFunction = Callable[[], list[PaperItem]]
SearchRepositoriesFunction = Callable[[], list[RepositoryItem]]


class DailyBriefAgent:
    """编排“检索新闻 -> Python 去重 -> LLM 编辑”的固定日报流程。"""

    def __init__(
        self,
        llm_client: LLMClient,
        search_news_func: SearchNewsFunction,
        search_papers_func: SearchPapersFunction | None = None,
        search_repositories_func: SearchRepositoriesFunction | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.search_news_func = search_news_func
        self.search_papers_func = search_papers_func
        self.search_repositories_func = search_repositories_func

    def generate_daily_brief(self) -> str:
        """返回一份基于当前新闻搜索结果生成的 Markdown 日报。"""
        raw_items = self.search_news_func()
        news_items = deduplicate_news_items(raw_items)
        paper_items = (
            deduplicate_items_by_url(self.search_papers_func())
            if self.search_papers_func is not None
            else []
        )
        repository_items = (
            deduplicate_items_by_url(self.search_repositories_func())
            if self.search_repositories_func is not None
            else []
        )
        source_json = json.dumps(
            {
                "news": news_items,
                "papers": paper_items,
                "repositories": repository_items,
            },
            ensure_ascii=False,
            indent=2,
        )
        prompt = self._build_prompt(source_json)

        return self.llm_client.generate(
            prompt=prompt,
            system_prompt=DAILY_BRIEF_SYSTEM_PROMPT,
        )

    @staticmethod
    def _build_prompt(source_json: str) -> str:
        """将结构化资料封装为给 LLM 的用户提示词。"""
        today = date.today().isoformat()

        return f"""请为 {today} 生成 AI 每日简报。

以下是唯一可使用的资料（JSON），其中 news 是技术新闻，papers 是学术论文，
repositories 是开源项目：

```json
{source_json}
```
"""
