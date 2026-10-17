from news_tools import (
    NewsToolError,
    deduplicate_news_items,
    search_ai_news,
)


def print_news_item(index: int, item: dict) -> None:
    """以便于人工检查的格式打印一条新闻。"""
    print(f"{index}. {item['title']}")
    print(f"   来源：{item['source']}")
    print(f"   时间：{item['published_at'] or '未提供'}")
    print(f"   摘要：{item['snippet'] or '未提供'}")
    print(f"   链接：{item['url']}")
    print()


def main() -> None:
    print("正在搜索过去 24 小时的 AI 新闻...\n")

    try:
        raw_items = search_ai_news(num_results=10)
        unique_items = deduplicate_news_items(raw_items)
    except NewsToolError as error:
        print(f"新闻搜索失败：{error}")
        return

    if not unique_items:
        print("没有检索到可用新闻。请稍后重试或调整查询词。")
        return

    print(f"共获取 {len(raw_items)} 条原始新闻，去重后剩余 {len(unique_items)} 条。\n")
    print("=" * 70)

    for index, item in enumerate(unique_items, start=1):
        print_news_item(index, item)


if __name__ == "__main__":
    main()