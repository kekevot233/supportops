"""使用真实配置运行 LangGraph 版 AI 日报。"""

from __future__ import annotations

from daily_brief_graph import DailyBriefGraph
from email_notifier import EmailNotificationError, send_daily_brief_email
from llm_client import OpenAICompatibleClient
from news_tools import NewsToolError, search_ai_news
from research_tools import (
    ResearchToolError,
    search_arxiv_papers,
    search_featured_agent_repositories,
)


def main() -> int:
    """运行日报图；任一关键环节失败都返回非零退出码。"""
    print("正在运行 LangGraph 版 AI 日报流程...")

    try:
        graph = DailyBriefGraph(
            llm_client=OpenAICompatibleClient(),
            search_news_func=search_ai_news,
            search_papers_func=search_arxiv_papers,
            search_repositories_func=search_featured_agent_repositories,
            send_email_func=send_daily_brief_email,
        )
        result = graph.run()
    except (NewsToolError, ResearchToolError, EmailNotificationError) as error:
        print(f"日报流程失败：{error}")
        return 1
    except (OSError, ValueError) as error:
        print(f"日报配置或文件操作失败：{error}")
        return 1

    if not result.get("validation_passed", False):
        print(result.get("error") or "日报未通过质量检查，未保存、未发送。")
        return 1

    output_path = result.get("output_path", "")
    print(f"日报已保存：{output_path}")

    if not result.get("email_sent", False):
        print(result.get("error") or "日报已保存，但邮件未发送。")
        return 1

    print("日报邮件已发送。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
