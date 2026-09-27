"""Per-outlet adapter registry (docs/plans/newspaper_plan.md §2, §11).

Each adapter module exposes:
    discover_urls(outlet, keyword, max_results) -> list[str]
    extract_meta(html) -> (title, body_text, ids_or_None)
    fetch_comments(outlet, ids, page_size, max_comments) -> Iterator[dict]

fetch_comments yields NORMALIZED comment dicts -- native_comment_id, content
(already cleaned), create_time (unix epoch), likes_count, author_name,
is_pinned -- so shared code in news_common.to_record never needs to know
any outlet's native field names. body_text from extract_meta is for gating
only (plan §5); it is never stored on an output record.

Adding an outlet is adding a module here + one line below, not touching
discover_news.py/crawl_news.py.
"""

from crawlers.news.adapters import thanhnien, vietnamnet, vnexpress

ADAPTERS = {
    "vnexpress": vnexpress,
    "thanhnien": thanhnien,
    "vietnamnet": vietnamnet,
}
