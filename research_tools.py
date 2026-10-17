"""为 AI 日报获取 arXiv 论文和 GitHub 开源项目。"""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import TypedDict

import requests
from dotenv import load_dotenv


load_dotenv()


ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_REQUEST_TIMEOUT_SECONDS = 30
ARXIV_MAX_ATTEMPTS = 2
ARXIV_RETRY_DELAY_SECONDS = 3.0
ARXIV_USER_AGENT = "HelloAgent-DailyBrief/1.0 (educational project)"
GITHUB_REPOSITORIES_SEARCH_URL = "https://api.github.com/search/repositories"
DEFAULT_ARXIV_QUERY = "all:AI"
DEFAULT_GITHUB_QUERY = "AI"
DEFAULT_AGENT_PROJECT_QUERIES = (
    '"AI agent"',
    '"LLM agent"',
    '"agent framework"',
)
DEFAULT_MINIMUM_STARS = 500
DEFAULT_ACTIVE_DAYS = 30
GITHUB_CANDIDATES_PER_QUERY = 20


class PaperItem(TypedDict):
    """日报流程内部使用的一条标准化 arXiv 论文数据。"""

    title: str
    url: str
    summary: str
    authors: str
    published_at: str
    category: str


class RepositoryItem(TypedDict):
    """日报流程内部使用的一条标准化 GitHub 仓库数据。"""

    title: str
    url: str
    summary: str
    stars: int
    language: str
    pushed_at: str
    is_fork: bool
    is_archived: bool
    category: str


class ResearchToolError(RuntimeError):
    """论文或开源项目获取失败时抛出的统一异常。"""


def _normalize_text(value: str | None) -> str:
    """把 XML 文本中的换行和连续空白整理为一个空格。"""
    return " ".join((value or "").split())


def _validate_count(count: int, name: str) -> None:
    if not 1 <= count <= 20:
        raise ResearchToolError(f"{name} 必须在 1 到 20 之间。")


def _is_retryable_http_error(error: requests.HTTPError) -> bool:
    """只有限流和服务端临时故障值得再次请求。"""
    response = error.response
    if response is None:
        return False

    return response.status_code == 429 or 500 <= response.status_code < 600


def _get_retry_delay_seconds(
    error: requests.HTTPError,
    default_delay_seconds: float,
) -> float:
    """优先遵守服务端 Retry-After；异常值则使用本地默认等待时间。"""
    response = error.response
    if response is None:
        return default_delay_seconds

    try:
        retry_after = float(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return default_delay_seconds

    return max(default_delay_seconds, min(retry_after, 60.0))


def search_arxiv_papers(
    query: str = DEFAULT_ARXIV_QUERY,
    max_results: int = 5,
    max_attempts: int = ARXIV_MAX_ATTEMPTS,
    retry_delay_seconds: float = ARXIV_RETRY_DELAY_SECONDS,
    timeout_seconds: int = ARXIV_REQUEST_TIMEOUT_SECONDS,
) -> list[PaperItem]:
    """按提交时间倒序搜索 arXiv 论文，并有限重试网络故障。"""
    query = query.strip()

    if not query:
        raise ResearchToolError("arXiv query 不能为空。")

    _validate_count(max_results, "max_results")

    if not 1 <= max_attempts <= ARXIV_MAX_ATTEMPTS:
        raise ResearchToolError(
            f"max_attempts 必须在 1 到 {ARXIV_MAX_ATTEMPTS} 之间。"
        )

    if retry_delay_seconds < 0:
        raise ResearchToolError("retry_delay_seconds 不能小于 0。")

    if timeout_seconds < 1:
        raise ResearchToolError("timeout_seconds 必须至少为 1。")

    params = {
        "search_query": query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }

    root = None
    last_network_error: requests.RequestException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(
                ARXIV_API_URL,
                params=params,
                headers={"User-Agent": ARXIV_USER_AGENT},
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
            break
        except (requests.Timeout, requests.ConnectionError) as error:
            last_network_error = error
            if attempt < max_attempts:
                time.sleep(retry_delay_seconds)
        except requests.HTTPError as error:
            if not _is_retryable_http_error(error):
                raise ResearchToolError("arXiv 请求或响应解析失败。") from error

            last_network_error = error
            if attempt < max_attempts:
                time.sleep(
                    _get_retry_delay_seconds(error, retry_delay_seconds)
                )
        except (requests.RequestException, ET.ParseError) as error:
            raise ResearchToolError("arXiv 请求或响应解析失败。") from error

    if root is None:
        raise ResearchToolError(
            f"arXiv 网络或服务请求失败，已尝试 {max_attempts} 次。"
        ) from last_network_error

    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    papers: list[PaperItem] = []

    for entry in root.findall("atom:entry", namespace):
        title = _normalize_text(entry.findtext("atom:title", default="", namespaces=namespace))
        url = _normalize_text(entry.findtext("atom:id", default="", namespaces=namespace))

        if not title or not url:
            continue

        author_names = [
            _normalize_text(author.findtext("atom:name", default="", namespaces=namespace))
            for author in entry.findall("atom:author", namespace)
        ]
        papers.append(
            {
                "title": title,
                "url": url,
                "summary": _normalize_text(
                    entry.findtext("atom:summary", default="", namespaces=namespace)
                ),
                "authors": ", ".join(name for name in author_names if name),
                "published_at": _normalize_text(
                    entry.findtext("atom:published", default="", namespaces=namespace)
                ),
                "category": "paper",
            }
        )

    return papers


def search_github_repositories(
    query: str = DEFAULT_GITHUB_QUERY,
    max_results: int = 5,
    github_token: str | None = None,
    sort: str = "updated",
) -> list[RepositoryItem]:
    """搜索公开 GitHub 仓库，并返回标准化数据。"""
    query = query.strip()

    if not query:
        raise ResearchToolError("GitHub query 不能为空。")

    _validate_count(max_results, "max_results")

    if sort not in {"stars", "updated"}:
        raise ResearchToolError("GitHub sort 只能是 stars 或 updated。")

    token = github_token or os.getenv("GITHUB_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    params = {
        "q": query,
        "sort": sort,
        "order": "desc",
        "per_page": max_results,
    }

    try:
        response = requests.get(
            GITHUB_REPOSITORIES_SEARCH_URL,
            params=params,
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as error:
        raise ResearchToolError("GitHub 请求或响应解析失败。") from error

    repositories: list[RepositoryItem] = []

    for raw_item in data.get("items", []):
        title = _normalize_text(str(raw_item.get("full_name", "")))
        url = _normalize_text(str(raw_item.get("html_url", "")))

        if not title or not url:
            continue

        repositories.append(
            {
                "title": title,
                "url": url,
                "summary": _normalize_text(raw_item.get("description")),
                "stars": int(raw_item.get("stargazers_count") or 0),
                "language": _normalize_text(raw_item.get("language")) or "未提供",
                "pushed_at": _normalize_text(raw_item.get("pushed_at")),
                "is_fork": bool(raw_item.get("fork")),
                "is_archived": bool(raw_item.get("archived")),
                "category": "repository",
            }
        )

    return repositories


def _was_pushed_within_days(pushed_at: str, cutoff_date: date) -> bool:
    """判断 GitHub 的 ISO 8601 推送时间是否不早于截止日期。"""
    try:
        pushed_date = date.fromisoformat(pushed_at[:10])
    except ValueError:
        return False

    return pushed_date >= cutoff_date


def search_featured_agent_repositories(
    max_results: int = 5,
    min_stars: int = DEFAULT_MINIMUM_STARS,
    active_days: int = DEFAULT_ACTIVE_DAYS,
    github_token: str | None = None,
    today: date | None = None,
) -> list[RepositoryItem]:
    """获取高星、活跃且与 Agent 相关的 GitHub 项目。"""
    _validate_count(max_results, "max_results")

    if min_stars < 0:
        raise ResearchToolError("min_stars 不能小于 0。")

    if active_days < 1:
        raise ResearchToolError("active_days 必须至少为 1。")

    reference_date = today or date.today()
    cutoff_date = reference_date - timedelta(days=active_days)
    candidates: list[RepositoryItem] = []

    for keyword_query in DEFAULT_AGENT_PROJECT_QUERIES:
        query = (
            f"{keyword_query} stars:>={min_stars} "
            f"pushed:>={cutoff_date.isoformat()} archived:false fork:false"
        )
        candidates.extend(
            search_github_repositories(
                query=query,
                max_results=GITHUB_CANDIDATES_PER_QUERY,
                github_token=github_token,
                sort="stars",
            )
        )

    eligible_items = [
        item
        for item in deduplicate_items_by_url(candidates)
        if item["stars"] >= min_stars
        and not item["is_fork"]
        and not item["is_archived"]
        and _was_pushed_within_days(item["pushed_at"], cutoff_date)
    ]

    return sorted(
        eligible_items,
        key=lambda item: (item["stars"], item["pushed_at"]),
        reverse=True,
    )[:max_results]


def deduplicate_items_by_url[T: dict](items: list[T]) -> list[T]:
    """按 URL 去重，供论文与项目等同样含有 url 字段的资料使用。"""
    seen_urls: set[str] = set()
    unique_items: list[T] = []

    for item in items:
        normalized_url = str(item["url"]).rstrip("/")
        if normalized_url in seen_urls:
            continue

        seen_urls.add(normalized_url)
        unique_items.append(item)

    return unique_items
