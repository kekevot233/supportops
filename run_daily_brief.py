"""使用真实 SerpApi 和 DeepSeek 配置生成一份 AI 每日简报。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from daily_brief_agent import DailyBriefAgent
from email_notifier import EmailNotificationError, send_daily_brief_email
from llm_client import OpenAICompatibleClient
from news_tools import NewsToolError, search_ai_news
from research_tools import (
    ResearchToolError,
    search_arxiv_papers,
    search_featured_agent_repositories,
)


def main() -> int:
    """生成日报，并保存为 outputs/daily_briefs/YYYY-MM-DD.md。"""
    print("正在搜索过去 24 小时的 AI 新闻...")

    agent = DailyBriefAgent(
        llm_client=OpenAICompatibleClient(),
        search_news_func=search_ai_news,
        search_papers_func=search_arxiv_papers,
        search_repositories_func=search_featured_agent_repositories,
    )

    try:
        brief = agent.generate_daily_brief()
    except (NewsToolError, ResearchToolError) as error:
        print(f"资料获取失败，未生成日报：{error}")
        return 1

    # 当前 llm_client 的错误处理会返回错误文本，因此不把失败信息保存为日报。
    if brief.startswith("错误："):
        print("语言模型调用失败，未生成日报。")
        return 1

    output_dir = Path("outputs") / "daily_briefs"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{date.today().isoformat()}.md"
    output_path.write_text(brief, encoding="utf-8")

    print(f"日报已保存：{output_path}")

    try:
        send_daily_brief_email(output_path)
    except EmailNotificationError as error:
        print(f"日报已保存，但邮件未发送：{error}")
        return 1

    print("日报邮件已发送。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
