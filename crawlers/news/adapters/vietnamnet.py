"""VietnamNet adapter. Mechanism confirmed live via a spike 2026-08-09.

Two structural differences from VnExpress/Thanh Nien that shaped this file:

1. **Search is bot-protected, discovery uses RSS instead.** `/tim-kiem?q=`
   is served differently to a real browser (genuine query-filtered results,
   confirmed live via screenshot: header "KET QUA TIM KIEM", or "Khong tim
   thay ket qua phu hop" when empty) than to a plain HTTP client (curl/
   urllib always get a generic fallback page, regardless of query --
   confirmed not a caching artifact by retrying with cache-busting params).
   This is specific to that one route; article pages and the comment API
   are both open, unprotected plain HTTP. Rather than add a browser-
   automation dependency for this one outlet, discovery instead pulls a
   fixed set of category RSS feeds (`outlet["rss_categories"]`, all
   confirmed live: thoi-su, giao-thong, doi-song) and filters items by
   keyword match on title+description client-side. RSS is category-scoped,
   not keyword-scoped, so items are fetched+cached once per run (stashed on
   the mutable `outlet` dict -- same pattern as adapters/thanhnien.py's
   reply cache) rather than re-fetched on every one of the ~77 keyword
   calls discover_news.py makes.

2. **Comments are a clean JSON REST API, and replies reuse the SAME
   endpoint.** `GET https://comment.vietnamnet.vn/api/Comment/Gets
   ?ObjectId=<id>&WebsiteId=000003&PageIndex=<n>&PageSize=<n>` -- no HTML
   scraping needed, unlike Thanh Nien. Adding `&ParentId=<parent commentId>`
   to the same call returns that comment's replies (confirmed live: a
   reply item comes back with parentId/answerId both set to the parent's
   commentId) -- no separate reply endpoint, unlike VnExpress's
   getreplay.htm.
"""

import html as html_lib
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from crawlers.news.news_common import NewsAPIError, fetch_json, fetch_text

_HCM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# -- RSS parsing (discovery) --------------------------------------------
_ITEM_PATTERN = re.compile(r"<item>.*?</item>", re.DOTALL)
_LINK_PATTERN = re.compile(r"<link>(.*?)</link>")
_RSS_TITLE_PATTERN = re.compile(r"<title><!\[CDATA\[(.*?)\]\]></title>")
_RSS_DESC_PATTERN = re.compile(r"<description><!\[CDATA\[(.*?)\]\]></description>", re.DOTALL)
_TAG_PATTERN = re.compile(r"<[^>]+>")

# -- Article page parsing -------------------------------------------------
_TITLE_TAG_PATTERN = re.compile(r"<title>([^<]*)</title>")
_OBJECTID_PATTERN = re.compile(r'data-objectid="(\d+)"')
_WEBSITEID_PATTERN = re.compile(r'data-websiteid="(\d+)"')
# JSON-LD NewsArticle field -- confirmed live 2026-08-09. Comes through as
# "<date>.000 +07:00" (millis, and a space before the offset) rather than
# the clean "+07:00"-suffixed ISO 8601 VnExpress/Thanh Nien give -- see
# _normalize_published_at.
_DATE_PUBLISHED_PATTERN = re.compile(r'"datePublished"\s*:\s*"([^"]*)"')
_MILLIS_SPACE_PATTERN = re.compile(r"\.\d{3}\s+(?=[+-]\d{2}:\d{2}$)")
# Article body sits between these two markers -- id="comment" is the
# comment-widget div confirmed live right after the body, giving a clean
# boundary without needing to count nested divs (same reasoning as Thanh
# Nien's body_start/body_end markers).
_BODY_START_PATTERN = re.compile(r'id="maincontent"[^>]*>')
_BODY_END_PATTERN = re.compile(r'id="comment"')


def _get_rss_items(outlet):
    """Fetches+parses outlet["rss_categories"] once per run, caching the
    combined deduped item list on the outlet dict itself (mutable, shared
    across every discover_urls() call this run makes -- see module
    docstring). Each item is {"url", "title", "description"}."""
    if "_rss_items_cache" in outlet:
        return outlet["_rss_items_cache"]
    items = []
    seen_links = set()
    for category in outlet["rss_categories"]:
        url = f"https://vietnamnet.vn/{category}.rss"
        try:
            xml_text = fetch_text(url)
        except (OSError, ValueError):
            continue
        for item_match in _ITEM_PATTERN.finditer(xml_text):
            block = item_match.group(0)
            link_match = _LINK_PATTERN.search(block)
            if not link_match:
                continue
            link = link_match.group(1).strip()
            if link in seen_links:
                continue
            seen_links.add(link)
            title_match = _RSS_TITLE_PATTERN.search(block)
            desc_match = _RSS_DESC_PATTERN.search(block)
            title = html_lib.unescape(title_match.group(1)).strip() if title_match else ""
            description = ""
            if desc_match:
                description = re.sub(r"\s+", " ", _TAG_PATTERN.sub(" ", desc_match.group(1))).strip()
                description = html_lib.unescape(description)
            items.append({"url": link, "title": title, "description": description})
    outlet["_rss_items_cache"] = items
    return items


def discover_urls(outlet, keyword, max_results):
    items = _get_rss_items(outlet)
    pattern = outlet["article_url_pattern"]
    keyword_lower = keyword.lower()
    matches = []
    for item in items:
        if not pattern.match(item["url"]):
            continue
        haystack = f"{item['title']} {item['description']}".lower()
        if keyword_lower not in haystack:
            continue
        matches.append(item["url"])
        if len(matches) >= max_results:
            break
    return matches


def _normalize_published_at(raw):
    """"2026-07-30T15:11:00.000 +07:00" -> "2026-07-30T15:11:00+07:00",
    matching the clean ISO 8601 shape VnExpress/Thanh Nien already give
    (millis and the space before the offset are dropped)."""
    if not raw:
        return ""
    without_millis_space = _MILLIS_SPACE_PATTERN.sub("", raw)
    return without_millis_space.replace(".000", "")


def extract_meta(html):
    """(title, body_text, published_at, ids_or_None). ids is None if the
    page has no data-objectid/data-websiteid pair (no comment widget) --
    not an error."""
    title_match = _TITLE_TAG_PATTERN.search(html or "")
    title = html_lib.unescape(title_match.group(1)).strip() if title_match else ""

    body_text = ""
    start_match = _BODY_START_PATTERN.search(html or "")
    if start_match:
        end_match = _BODY_END_PATTERN.search(html, start_match.end())
        raw_body = html[start_match.end():end_match.start()] if end_match else html[start_match.end():]
        body_text = re.sub(r"\s+", " ", _TAG_PATTERN.sub(" ", raw_body)).strip()

    date_match = _DATE_PUBLISHED_PATTERN.search(html or "")
    published_at = _normalize_published_at(date_match.group(1)) if date_match else ""

    objectid_match = _OBJECTID_PATTERN.search(html or "")
    websiteid_match = _WEBSITEID_PATTERN.search(html or "")
    if not objectid_match or not websiteid_match:
        return title, body_text, published_at, None
    return title, body_text, published_at, {
        "object_id": objectid_match.group(1), "website_id": websiteid_match.group(1),
    }


def _clean_text(raw):
    text = _TAG_PATTERN.sub(" ", raw or "")
    text = re.sub(r"\s+", " ", text).strip()
    return html_lib.unescape(text)


def _parse_time(date_str):
    """"YYYY-MM-DDTHH:MM:SS" (confirmed live, no tz suffix -- site-local
    Asia/Ho_Chi_Minh, same assumption as Thanh Nien's time-ago field) ->
    unix epoch seconds. Returns None if unparseable."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_HCM_TZ)
    except (TypeError, ValueError):
        return None
    return int(dt.timestamp())


def normalize_comment(item):
    """Raw Comment/Gets item -> normalized dict (adapters/__init__.py).
    Pure and offline-testable -- no network."""
    return {
        "native_comment_id": item.get("commentId"),
        "content": _clean_text(item.get("commentContent", "")),
        "create_time": _parse_time(item.get("createdDate")),
        "likes_count": item.get("totalLike", 0),
        "author_name": html_lib.unescape(item.get("userName") or "").strip(),
        "is_pinned": bool(item.get("isPin", False)),
        "reply_count": item.get("totalAnswer", 0),
    }


def _fetch_comment_page(outlet, ids, page_index, page_size, parent_id=None):
    params = {
        "ObjectId": ids["object_id"], "WebsiteId": ids["website_id"],
        "PageIndex": page_index, "PageSize": page_size,
    }
    if parent_id is not None:
        params["ParentId"] = parent_id
    payload = fetch_json(outlet["comment_api"], params)
    if not payload.get("status", False):
        raise NewsAPIError(f"Comment/Gets error: {payload.get('messages')}")
    return payload.get("data", {})


def fetch_comments(outlet, ids, page_size, max_comments):
    """Yields normalized top-level comment dicts. The API has no explicit
    sort param confirmed live (returned newest-first in the spike) --
    unlike VnExpress/Thanh Nien this isn't like-count-sorted, so
    select_top_n-style ranking downstream matters more here.

    Paginates purely on "did this page come back empty", not any total
    field in the response -- confirmed live 2026-08-09 that `totalComment`
    counts top-level comments AND replies combined (a 20-top-level-comment
    article with one reply on one thread reported totalComment=21, then a
    second PageIndex for top-level comments came back empty), so using it
    as a top-level-pagination stop condition would either overrun or
    silently understate real content depending on the mix. `totalRow` in
    the response also isn't reliable as a total (it echoed the actual rows
    returned by ONE page, not a grand total)."""
    page = 0
    yielded = 0
    while yielded < max_comments:
        data = _fetch_comment_page(outlet, ids, page, page_size)
        comments = data.get("comments", [])
        if not comments:
            return
        for item in comments:
            if yielded >= max_comments:
                return
            yield normalize_comment(item)
            yielded += 1
        page += 1


def fetch_replies(outlet, ids, parent_comment_id, reply_total, page_size, max_replies):
    """Yields normalized reply dicts via the SAME Comment/Gets endpoint,
    filtered by ParentId -- confirmed live 2026-08-09, no separate
    reply-listing endpoint exists for this outlet (see module docstring).
    ``reply_total`` (the parent's own totalAnswer) is accepted for
    interface parity with the VnExpress/Thanh Nien adapters but unused --
    pagination stops on the first empty page instead, for the same reason
    fetch_comments() does (see its docstring)."""
    page = 0
    yielded = 0
    while yielded < max_replies:
        data = _fetch_comment_page(outlet, ids, page, page_size, parent_id=parent_comment_id)
        items = data.get("comments", [])
        if not items:
            return
        for item in items:
            if yielded >= max_replies:
                return
            yield normalize_comment(item)
            yielded += 1
        page += 1
