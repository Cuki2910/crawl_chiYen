"""VnExpress adapter. Mechanism confirmed live (plan §8 Findings): a plain,
unauthenticated JSON API (usi-saas.vnexpress.net), no browser needed, sorted
by like count descending by default.
"""

import json
import re
import urllib.parse

from crawlers.news.news_common import NewsAPIError, fetch_json, fetch_text

_TITLE_PATTERN = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)
_COMPONENT_INPUT_PATTERN = re.compile(r"data-component-input='(\{[^']*\})'")
# Article body container, confirmed live 2026-07-29 against a real article.
# Used for gating only (plan §5) -- text is discarded after, never stored.
_BODY_PATTERN = re.compile(r'<article class="fck_detail ?">(.*?)</article>', re.DOTALL)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_QUOTE_PREFIX_PATTERN = re.compile(r'^"[^"]*"<br\s*/?>\s*', re.IGNORECASE)
_BR_PATTERN = re.compile(r"<br\s*/?>", re.IGNORECASE)
# JSON-LD NewsArticle field, confirmed live 2026-07-29 on two real articles
# (different years) -- already ISO 8601 with a +07:00 offset, no Vietnamese
# date-string parsing needed.
_DATE_PUBLISHED_PATTERN = re.compile(r'"datePublished"\s*:\s*"([^"]*)"')


def discover_urls(outlet, keyword, max_results):
    url = outlet["search_url_template"].format(query=urllib.parse.quote(keyword))
    html = fetch_text(url)
    candidates = re.findall(r'href="(https://vnexpress\.net/[a-z0-9-]+\.html)"', html)
    pattern = outlet["article_url_pattern"]
    seen = []
    for href in candidates:
        if pattern.match(href) and href not in seen:
            seen.append(href)
        if len(seen) >= max_results:
            break
    return seen


def extract_meta(html):
    """(title, body_text, published_at, comment_ids_dict_or_None). ids is
    None if the page has no comment widget (e.g. some video/live pages) --
    not an error. published_at is "" if the JSON-LD field wasn't found."""
    title_match = _TITLE_PATTERN.search(html or "")
    title = title_match.group(1).strip() if title_match else ""
    title = re.sub(r"\s*-\s*(Báo\s+)?VnExpress\s*$", "", title).strip()

    body_match = _BODY_PATTERN.search(html or "")
    body_text = ""
    if body_match:
        body_text = re.sub(r"\s+", " ", _TAG_PATTERN.sub(" ", body_match.group(1))).strip()

    date_match = _DATE_PUBLISHED_PATTERN.search(html or "")
    published_at = date_match.group(1) if date_match else ""

    component_match = _COMPONENT_INPUT_PATTERN.search(html or "")
    if not component_match:
        return title, body_text, published_at, None
    try:
        component = json.loads(component_match.group(1))
    except json.JSONDecodeError:
        return title, body_text, published_at, None
    required = ("article_id", "article_type", "site_id", "category_id")
    if any(key not in component for key in required):
        return title, body_text, published_at, None
    return title, body_text, published_at, {key: component[key] for key in required}


def _clean_comment_text(raw_content):
    text = _QUOTE_PREFIX_PATTERN.sub("", raw_content or "")
    text = _BR_PATTERN.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_comment_page(comment_api, ids, limit, offset):
    params = {
        "objectid": ids["article_id"], "objecttype": ids["article_type"],
        "siteid": ids["site_id"], "categoryid": ids["category_id"],
        "limit": limit, "offset": offset,
    }
    payload = fetch_json(comment_api, params)
    if payload.get("error", 0) != 0:
        raise NewsAPIError(f"error={payload.get('error')} {payload.get('errorDescription', '')}")
    return payload["data"]


def _fetch_reply_page(reply_api, ids, parent_comment_id, limit, offset):
    params = {
        "siteid": ids["site_id"], "objectid": ids["article_id"], "objecttype": ids["article_type"],
        "id": parent_comment_id, "limit": limit, "offset": offset, "sort_by": "like",
    }
    payload = fetch_json(reply_api, params)
    if payload.get("error", 0) != 0:
        raise NewsAPIError(f"error={payload.get('error')} {payload.get('errorDescription', '')}")
    return payload["data"]


def normalize_comment(item):
    """Raw usi-saas comment item -> normalized dict (adapters/__init__.py).
    Pure and offline-testable -- no network. ``reply_count`` is 0 for reply
    items themselves (the "replys" field only appears on top-level items)."""
    return {
        "native_comment_id": item.get("comment_id"),
        "content": _clean_comment_text(item.get("content", "")),
        "create_time": item.get("creation_time"),
        "likes_count": item.get("userlike", 0),
        "author_name": item.get("full_name", ""),
        "is_pinned": bool(item.get("is_pin", 0)),
        "reply_count": (item.get("replys") or {}).get("total", 0),
    }


def fetch_comments(outlet, ids, page_size, max_comments):
    """Yields normalized **top-level** comment dicts (see
    adapters/__init__.py). Sorted by like count descending by default -- no
    client-side sort needed. Replies are fetched separately via
    fetch_replies() -- this endpoint's own "replys.items" is always empty
    (replies are loaded lazily by the live site), so reply_count is the only
    reply-related signal this function surfaces."""
    offset = 0
    total = None
    yielded = 0
    while total is None or offset < total:
        if yielded >= max_comments:
            return
        data = _fetch_comment_page(outlet["comment_api"], ids, page_size, offset)
        total = data.get("total", 0)
        items = data.get("items", [])
        if not items:
            return
        for item in items:
            if yielded >= max_comments:
                return
            yield normalize_comment(item)
            yielded += 1
        offset += page_size


def fetch_replies(outlet, ids, parent_comment_id, reply_total, page_size, max_replies):
    """Yields normalized reply dicts for one parent comment, via the
    separate index/getreplay endpoint confirmed live 2026-07-31 (see
    OUTLETS["vnexpress"]["reply_api"]). ``reply_total`` is the parent's own
    "replys": {"total": N} count from fetch_comments() -- this endpoint's
    response carries no total/has_more field of its own, so the caller-known
    count is the only stopping signal besides an empty page."""
    offset = 0
    yielded = 0
    while offset < reply_total:
        if yielded >= max_replies:
            return
        data = _fetch_reply_page(outlet["reply_api"], ids, parent_comment_id, page_size, offset)
        items = data.get("items", [])
        if not items:
            return
        for item in items:
            if yielded >= max_replies:
                return
            yield normalize_comment(item)
            yielded += 1
        offset += page_size
