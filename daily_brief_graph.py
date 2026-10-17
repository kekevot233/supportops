"""使用 LangGraph 编排 AI 每日简报的生成、校验、保存与通知。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Protocol, TypedDict
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from daily_brief_agent import (
    SearchNewsFunction,
    SearchPapersFunction,
    SearchRepositoriesFunction,
)
from news_tools import NewsItem, deduplicate_news_items
from prompts import DAILY_BRIEF_SYSTEM_PROMPT
from research_tools import PaperItem, RepositoryItem, deduplicate_items_by_url


MAX_GENERATION_ATTEMPTS = 2
REQUIRED_SECTIONS = (
    "AI 技术新闻",
    "AI 学术论文",
    "AI 开源项目",
)


class LLMClient(Protocol):
    """LangGraph 日报只依赖 LLM 的最小接口。"""

    def generate(self, prompt: str, system_prompt: str) -> str:
        """根据提示词返回完整文本。"""


class DailyBriefState(TypedDict, total=False):
    """图中所有节点共享的状态结构。"""

    news_items: list[NewsItem]
    paper_items: list[PaperItem]
    repository_items: list[RepositoryItem]
    source_json: str
    brief_markdown: str
    output_path: str
    validation_passed: bool
    validation_feedback: str
    retry_count: int
    email_sent: bool
    error: str


SendEmailFunction = Callable[[Path], None]
DateProvider = Callable[[], date]


class DailyBriefGraph:
    """可注入依赖、可测试的 AI 日报 LangGraph。"""

    def __init__(
        self,
        llm_client: LLMClient,
        search_news_func: SearchNewsFunction,
        search_papers_func: SearchPapersFunction,
        search_repositories_func: SearchRepositoriesFunction,
        send_email_func: SendEmailFunction,
        output_dir: Path = Path("outputs") / "daily_briefs",
        max_generation_attempts: int = MAX_GENERATION_ATTEMPTS,
        date_provider: DateProvider = date.today,
        checkpointer=None,
    ) -> None:
        if not 1 <= max_generation_attempts <= MAX_GENERATION_ATTEMPTS:
            raise ValueError(
                f"max_generation_attempts 必须在 1 到 {MAX_GENERATION_ATTEMPTS} 之间。"
            )

        self.llm_client = llm_client
        self.search_news_func = search_news_func
        self.search_papers_func = search_papers_func
        self.search_repositories_func = search_repositories_func
        self.send_email_func = send_email_func
        self.output_dir = output_dir
        self.max_generation_attempts = max_generation_attempts
        self.date_provider = date_provider
        self.checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
        self.app = self._build_graph()

    def _build_graph(self):
        """声明节点、普通边、条件边，并编译为可运行的图。"""
        workflow = StateGraph(DailyBriefState)

        workflow.add_node("search_news", self.search_news)
        workflow.add_node("search_papers", self.search_papers)
        workflow.add_node("search_repositories", self.search_repositories)
        workflow.add_node("build_source_data", self.build_source_data)
        workflow.add_node("generate_brief", self.generate_brief)
        workflow.add_node("validate_brief", self.validate_brief)
        workflow.add_node("save_brief", self.save_brief)
        workflow.add_node("send_email", self.send_email)

        workflow.add_edge(START, "search_news")
        workflow.add_edge("search_news", "search_papers")
        workflow.add_edge("search_papers", "search_repositories")
        workflow.add_edge("search_repositories", "build_source_data")
        workflow.add_edge("build_source_data", "generate_brief")
        workflow.add_edge("generate_brief", "validate_brief")
        workflow.add_conditional_edges(
            "validate_brief",
            self.route_after_validation,
            {
                "save": "save_brief",
                "retry": "generate_brief",
                "stop": END,
            },
        )
        workflow.add_edge("save_brief", "send_email")
        workflow.add_edge("send_email", END)

        return workflow.compile(checkpointer=self.checkpointer)

    def search_news(self, _state: DailyBriefState) -> dict:
        """搜索新闻并按 URL 去重。"""
        return {"news_items": deduplicate_news_items(self.search_news_func())}

    def search_papers(self, _state: DailyBriefState) -> dict:
        """搜索论文并按 URL 去重。"""
        return {
            "paper_items": deduplicate_items_by_url(self.search_papers_func())
        }

    def search_repositories(self, _state: DailyBriefState) -> dict:
        """搜索精选项目并按 URL 去重。"""
        return {
            "repository_items": deduplicate_items_by_url(
                self.search_repositories_func()
            )
        }

    def build_source_data(self, state: DailyBriefState) -> dict:
        """把三类结构化资料序列化为模型输入。"""
        source_json = json.dumps(
            {
                "news": state.get("news_items", []),
                "papers": state.get("paper_items", []),
                "repositories": state.get("repository_items", []),
            },
            ensure_ascii=False,
            indent=2,
        )
        return {"source_json": source_json}

    def generate_brief(self, state: DailyBriefState) -> dict:
        """生成日报；重试时把上一次确定性校验反馈交给模型。"""
        prompt = self._build_generation_prompt(state)
        brief = self.llm_client.generate(
            prompt=prompt,
            system_prompt=DAILY_BRIEF_SYSTEM_PROMPT,
        )
        return {
            "brief_markdown": brief or "",
            "retry_count": state.get("retry_count", 0) + 1,
            "validation_passed": False,
            "error": "",
        }

    def validate_brief(self, state: DailyBriefState) -> dict:
        """用确定性的 Python 规则检查栏目、错误文本和原始链接。"""
        problems: list[str] = []
        brief = state.get("brief_markdown", "").strip()

        if not brief:
            problems.append("日报内容为空")
        if brief.startswith("错误："):
            problems.append("日报以“错误：”开头，说明模型调用失败")

        missing_sections = [
            section for section in REQUIRED_SECTIONS if section not in brief
        ]
        if missing_sections:
            problems.append("缺少栏目：" + "、".join(missing_sections))

        missing_urls = [url for url in self._source_urls(state) if url not in brief]
        if missing_urls:
            problems.append("缺少原始链接：" + "、".join(missing_urls))

        validation_passed = not problems
        feedback = "；".join(problems)
        reached_limit = state.get("retry_count", 0) >= self.max_generation_attempts
        error = ""
        if not validation_passed and reached_limit:
            error = (
                f"日报连续 {state.get('retry_count', 0)} 次未通过质量检查："
                f"{feedback}"
            )

        return {
            "validation_passed": validation_passed,
            "validation_feedback": feedback,
            "error": error,
        }

    def route_after_validation(self, state: DailyBriefState) -> str:
        """条件边路由：合格则保存，未合格则重试或终止。"""
        if state.get("validation_passed", False):
            return "save"
        if state.get("retry_count", 0) < self.max_generation_attempts:
            return "retry"
        return "stop"

    def save_brief(self, state: DailyBriefState) -> dict:
        """只有校验合格的日报才会写入磁盘。"""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.output_dir / f"{self.date_provider().isoformat()}.md"
        output_path.write_text(state["brief_markdown"], encoding="utf-8")
        return {"output_path": str(output_path)}

    def send_email(self, state: DailyBriefState) -> dict:
        """发送已保存日报；邮件失败不回滚磁盘文件。"""
        output_path = Path(state["output_path"])
        try:
            self.send_email_func(output_path)
        except Exception as error:
            return {
                "email_sent": False,
                "error": f"日报已保存，但邮件发送失败：{error}",
            }

        return {"email_sent": True, "error": ""}

    def run(self, thread_id: str | None = None) -> DailyBriefState:
        """以完整初始状态运行一次日报图，并返回最终 State。"""
        initial_state: DailyBriefState = {
            "news_items": [],
            "paper_items": [],
            "repository_items": [],
            "source_json": "",
            "brief_markdown": "",
            "output_path": "",
            "validation_passed": False,
            "validation_feedback": "",
            "retry_count": 0,
            "email_sent": False,
            "error": "",
        }
        config = {
            "configurable": {
                "thread_id": thread_id or f"daily-brief-{uuid4()}"
            }
        }
        return self.app.invoke(initial_state, config=config)

    def _build_generation_prompt(self, state: DailyBriefState) -> str:
        prompt = f"""请为 {self.date_provider().isoformat()} 生成 AI 每日简报。

以下是唯一可使用的资料（JSON），其中 news 是技术新闻，papers 是学术论文，
repositories 是开源项目：

```json
{state["source_json"]}
```
"""
        feedback = state.get("validation_feedback", "").strip()
        if feedback:
            prompt += f"""

上一版没有通过质量检查，问题如下：
{feedback}

请根据相同资料重新生成一份完整日报，修复全部问题，不要解释修改过程。
"""
        return prompt

    @staticmethod
    def _source_urls(state: DailyBriefState) -> list[str]:
        """按资料顺序收集所有必须出现在日报中的唯一原始链接。"""
        urls: list[str] = []
        for key in ("news_items", "paper_items", "repository_items"):
            for item in state.get(key, []):
                url = str(item.get("url", "")).strip()
                if url and url not in urls:
                    urls.append(url)
        return urls
