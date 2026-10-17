# news_tools.py
from __future__ import annotations

import os
import re
from typing import TypedDict
from urllib.parse import urlparse

from dotenv import load_dotenv
from serpapi import GoogleSearch


# 让本文件也能直接读取项目中的 .env。
# 如果你的主程序已经调用过 load_dotenv()，再次调用也没有问题。
load_dotenv()


class NewsItem(TypedDict):
    """日报流程内部使用的一条标准化新闻数据。"""

    title: str
    url: str
    snippet: str
    source: str
    published_at: str
    category: str


class NewsToolError(RuntimeError):
    """新闻工具调用失败时抛出的统一异常。"""


DEFAULT_AI_NEWS_QUERY = "人工智能 OR 大模型 OR LLM OR Agent OR AIGC"


def _safe_error_message(error: Exception) -> str:
    """脱敏第三方库异常中的 API Key，避免日志或终端输出泄露密钥。"""
    return re.sub(r"(?i)(api_key=)[^&\s]+", r"\1***", str(error))


def _get_serpapi_key(api_key: str | None = None) -> str:
    """
    获取 SerpApi Key。

    优先级：
    1. 调用函数时显式传入的 api_key
    2. .env 中的 SERPAPI_API_KEY
    3. .env 中的 SERPAPI_KEY
    """
    key = api_key or os.getenv("SERPAPI_API_KEY") or os.getenv("SERPAPI_KEY")

    if not key:
        raise NewsToolError(
            "未找到 SerpApi Key。请在 .env 中设置 SERPAPI_API_KEY，"
            "或在调用 search_ai_news() 时传入 api_key。"
        )

    return key


def _get_source_name(url: str, raw_source: str | None) -> str:
    """优先使用 SerpApi 给出的媒体名称；没有时从 URL 推导域名。"""
    if raw_source:
        return raw_source.strip()

    hostname = urlparse(url).netloc
    return hostname.removeprefix("www.") if hostname else "未知来源"


def _normalize_news_result(raw_item: dict) -> NewsItem | None:
    """
    将 SerpApi 的一条原始新闻结果规范化。

    缺少标题或链接的结果不能作为日报素材，因此返回 None。
    """
    title = str(raw_item.get("title", "")).strip()
    url = str(raw_item.get("link", "")).strip()

    if not title or not url:
        return None

    snippet = str(raw_item.get("snippet", "")).strip()
    published_at = str(raw_item.get("date", "")).strip()
    raw_source = raw_item.get("source")

    news_item: NewsItem = {
        "title": title,
        "url": url,
        "snippet": snippet,
        "source": _get_source_name(url, raw_source),
        "published_at": published_at,
        "category": "news",
    }
    return news_item


def search_ai_news(
    query: str = DEFAULT_AI_NEWS_QUERY,
    num_results: int = 10,
    api_key: str | None = None,
) -> list[NewsItem]:
    """
    搜索过去 24 小时的 AI 新闻，并返回结构化结果。

    参数：
        query: 搜索关键词。
        num_results: 最多请求多少条结果，范围为 1 到 20。
        api_key: 可选。一般不传，自动从 .env 读取。

    返回：
        list[NewsItem]，即标准化新闻字典的列表。

    异常：
        NewsToolError：Key 缺失、参数错误或 SerpApi 请求失败。
    """
    query = query.strip()

    if not query:
        raise NewsToolError("query 不能为空。")

    if not 1 <= num_results <= 20:
        raise NewsToolError("num_results 必须在 1 到 20 之间。")

    key = _get_serpapi_key(api_key)

    params = {
        "engine": "google",
        "tbm": "nws",       # Google News 模式
        "tbs": "qdr:d",     # 过去 24 小时
        "q": query,
        "num": num_results,
        "hl": "zh-cn",
        "gl": "cn",
        "api_key": key,
    }

    try:
        data = GoogleSearch(params).get_dict()
    except Exception as error:
        safe_message = _safe_error_message(error)
        raise NewsToolError(f"SerpApi 请求失败：{safe_message}") from error

    if "error" in data:
        raise NewsToolError(f"SerpApi 返回错误：{data['error']}")

    items: list[NewsItem] = []

    for raw_item in data.get("news_results", []):
        normalized_item = _normalize_news_result(raw_item)

        if normalized_item is not None:
            items.append(normalized_item)

    return items


def deduplicate_news_items(items: list[NewsItem]) -> list[NewsItem]:
    """
    按 URL 去重，并保持第一次出现的顺序。

    这一步由 Python 完成，而不是让 LLM 判断，
    因此结果稳定、可测试。
    """
    seen_urls: set[str] = set()
    unique_items: list[NewsItem] = []

    for item in items:
        normalized_url = item["url"].rstrip("/")

        if normalized_url in seen_urls:
            continue

        seen_urls.add(normalized_url)
        unique_items.append(item)

    return unique_items
