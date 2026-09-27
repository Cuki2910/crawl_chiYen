"""Thanh Nien adapter. Mechanism confirmed live via a spike 2026-08-09:
comments run through a shared platform called CNND (``eth2.cnnd.vn``), not
VnExpress's usi-saas -- and unlike VnExpress, this one is an HTML-fragment
API (``dataType: "html"`` in the site's own JS), not JSON. See
docs/plans/newspaper_plan.md Findings (Thanh Nien) for the full spike notes.

Search uses a `keywords=` query param, not `q=` -- confirmed by inspecting
the homepage's search box, which server-renders /tim-kiem.htm?q=... but
silently ignores that param (falls back to the homepage feed); the real
param name is read off the page's own canonical search-action template
(`/tim-kiem.htm?keywords={search_term_string}`).

Replies are the other structural difference from VnExpress: they are
inlined in the SAME ListComment.htm response, nested inside each top-level
comment's `.box-reply` div -- there is no separate reply-listing GET
endpoint. (`insertsubcomment.htm`, found in the site's JS bundle, is
POST-only -- for submitting a new reply, not reading existing ones.)
fetch_comments() therefore parses the full page once and stashes each
top-level comment's already-known replies on
``ids["_replies_by_parent"]``; fetch_replies() just reads that cache back
out afterward, no extra network call. This relies on crawl_news.py's
existing call shape -- comments = list(adapter.fetch_comments(...)) fully
drains the generator (and therefore finishes populating the cache) before
any fetch_replies() call is made for that same article.
"""

import html as html_lib
import re
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

from crawlers.news.news_common import NewsAPIError, fetch_text

_ARTICLE_LINK_PATTERN = re.compile(r'href="(/[a-z0-9-]+-\d{15,19}\.htm)"')
_HD_NEWS_ID_PATTERN = re.compile(r"id='hdNewsId' value='(\d+)'")
_HD_TITLE_PATTERN = re.compile(r"id='hdTitle' value='([^']*)'")
# JSON-LD NewsArticle field -- same shape as VnExpress's, already ISO 8601
# with a +07:00 offset.
_DATE_PUBLISHED_PATTERN = re.compile(r'"datePublished"\s*:\s*"([^"]*)"')
# Article body sits between two marker divs that don't nest (confirmed live
# 2026-08-09) -- more robust than matching a closing </div> for the body
# container itself, which would stop at the first nested div's close tag.
_BODY_PATTERN = re.compile(
    r'data-check-position="body_start".*?data-check-position="body_end"', re.DOTALL,
)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_MENTION_PREFIX_PATTERN = re.compile(r'<b class="username-reply">[^<]*</b>\s*')

# One top-level comment block runs from its own "item box_cm" div up to the
# next one (or end of page) -- safe because every reply nested under it
# (inside its .box-reply) closes before the next top-level item starts.
_TOP_ITEM_PATTERN = re.compile(
    r'<div class="item box_cm" data-cmid="([0-9a-f-]+)".*?'
    r'(?=<div class="item box_cm" data-cmid="|\Z)',
    re.DOTALL,
)
# Same lazy-lookahead-to-next-sibling shape as _TOP_ITEM_PATTERN, rather
# than counting closing </div> tags (fragile against markup drift) --
# reply blocks are siblings inside one top item's already-bounded `block`,
# so grabbing up to the next item-reply (or end of block) is safe.
_ITEM_REPLY_PATTERN = re.compile(
    r'<div class="item-reply">.*?(?=<div class="item-reply">|\Z)', re.DOTALL,
)
_AVATAR_TITLE_PATTERN = re.compile(r'class="avatar" title="([^"]*)"')
_TEXT_COMMENT_PATTERN = re.compile(r'<p class="text-comment[^"]*">(.*?)</p>', re.DOTALL)
_TOTAL_LIKE_PATTERN = re.compile(r'<span class="total-like">(\d+)</span>')
_TIME_AGO_PATTERN = re.compile(r'<span class="time time-ago" title="([^"]*)"')
_REPLY_LIKE_CMID_PATTERN = re.compile(r'class="item-ls like like-action" data-cmid="([0-9a-f-]+)"')

_HCM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def discover_urls(outlet, keyword, max_results):
    url = outlet["search_url_template"].format(query=urllib.parse.quote(keyword))
    html = fetch_text(url)
    pattern = outlet["article_url_pattern"]
    seen = []
    for href in _ARTICLE_LINK_PATTERN.findall(html):
        full_url = "https://thanhnien.vn" + href
        if not pattern.match(full_url) or full_url in seen:
            continue
        seen.append(full_url)
        if len(seen) >= max_results:
            break
    return seen


def _clean_text(raw):
    text = _MENTION_PREFIX_PATTERN.sub("", raw or "")
    text = _TAG_PATTERN.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return html_lib.unescape(text)


def _clean_author(raw):
    # Avatar `title` attrs come through numeric-entity-encoded (e.g.
    # "B&#x1EA1;n &#x111;&#x1ECD;c m&#x1EDB;i" for "Bạn đọc mới") -- the
    # fragment response isn't UTF-8-decoded HTML in the usual sense, it's
    # entity-escaped ASCII, confirmed live 2026-08-09.
    return html_lib.unescape(raw or "").strip()


def _parse_time(time_str):
    """"MM/DD/YYYY HH:MM:SS" (confirmed live, site-local Asia/Ho_Chi_Minh) ->
    unix epoch seconds. Returns None if unparseable -- iso_utc_from_epoch
    (crawlers/common/records.py) already treats that as a soft failure."""
    try:
        dt = datetime.strptime(time_str, "%m/%d/%Y %H:%M:%S").replace(tzinfo=_HCM_TZ)
    except (TypeError, ValueError):
        return None
    return int(dt.timestamp())


def extract_meta(html):
    """(title, body_text, published_at, ids_or_None). ids is None if the
    page has no hdNewsId field (no comment widget, e.g. some live/video
    pages) -- not an error. published_at is "" if the JSON-LD field wasn't
    found."""
    title_match = _HD_TITLE_PATTERN.search(html or "")
    title = html_lib.unescape(title_match.group(1)).strip() if title_match else ""

    body_match = _BODY_PATTERN.search(html or "")
    body_text = ""
    if body_match:
        body_text = re.sub(r"\s+", " ", _TAG_PATTERN.sub(" ", body_match.group(0))).strip()

    date_match = _DATE_PUBLISHED_PATTERN.search(html or "")
    published_at = date_match.group(1) if date_match else ""

    news_id_match = _HD_NEWS_ID_PATTERN.search(html or "")
    if not news_id_match:
        return title, body_text, published_at, None
    return title, body_text, published_at, {"news_id": news_id_match.group(1)}


def _parse_reply_block(block):
    like_match = _REPLY_LIKE_CMID_PATTERN.search(block)
    avatar_match = _AVATAR_TITLE_PATTERN.search(block)
    text_match = _TEXT_COMMENT_PATTERN.search(block)
    like_count_match = _TOTAL_LIKE_PATTERN.search(block)
    time_match = _TIME_AGO_PATTERN.search(block)
    return {
        "native_comment_id": like_match.group(1) if like_match else None,
        "content": _clean_text(text_match.group(1)) if text_match else "",
        "create_time": _parse_time(time_match.group(1)) if time_match else None,
        "likes_count": int(like_count_match.group(1)) if like_count_match else 0,
        "author_name": _clean_author(avatar_match.group(1)) if avatar_match else "",
        "is_pinned": False,
        "reply_count": 0,
    }


def _parse_top_item(block, native_comment_id):
    avatar_match = _AVATAR_TITLE_PATTERN.search(block)
    text_match = _TEXT_COMMENT_PATTERN.search(block)
    like_count_match = _TOTAL_LIKE_PATTERN.search(block)
    time_match = _TIME_AGO_PATTERN.search(block)
    replies = [_parse_reply_block(m.group(0)) for m in _ITEM_REPLY_PATTERN.finditer(block)]
    comment = {
        "native_comment_id": native_comment_id,
        "content": _clean_text(text_match.group(1)) if text_match else "",
        "create_time": _parse_time(time_match.group(1)) if time_match else None,
        "likes_count": int(like_count_match.group(1)) if like_count_match else 0,
        "author_name": _clean_author(avatar_match.group(1)) if avatar_match else "",
        "is_pinned": False,
        "reply_count": len(replies),
    }
    return comment, replies


def fetch_comments(outlet, ids, page_size, max_comments):
    """Yields normalized top-level comment dicts, sorted by like count
    descending (``Order=like``, the site's own default). As a side effect,
    stashes each comment's already-parsed replies on
    ``ids["_replies_by_parent"]`` -- see module docstring. Pagination stops
    on the first page that yields zero comments (an article with no
    comments returns a 0-byte response for page 1; a later page past the
    end returns an empty fragment the same way)."""
    ids.setdefault("_replies_by_parent", {})
    page = 1
    yielded = 0
    while yielded < max_comments:
        params = {
            "page": page, "NewsID": ids["news_id"], "Order": "like",
            "pageSize": page_size, "iObjectType": 1, "SiteName": outlet["site_name"],
        }
        url = f"{outlet['comment_api']}?{urllib.parse.urlencode(params)}"
        try:
            fragment = fetch_text(url)
        except (OSError, ValueError) as exc:
            raise NewsAPIError(f"ListComment fetch failed: {exc!r}") from exc
        fragment = (fragment or "").strip()
        if not fragment:
            return
        items = list(_TOP_ITEM_PATTERN.finditer(fragment))
        if not items:
            return
        for match in items:
            if yielded >= max_comments:
                return
            comment, replies = _parse_top_item(match.group(0), match.group(1))
            ids["_replies_by_parent"][comment["native_comment_id"]] = replies
            yield comment
            yielded += 1
        page += 1


def fetch_replies(outlet, ids, parent_comment_id, reply_total, page_size, max_replies):
    """Reads replies already parsed by fetch_comments() out of
    ``ids["_replies_by_parent"]`` -- no extra network call (see module
    docstring). ``reply_total``/``page_size`` are accepted for interface
    parity with the VnExpress adapter but unused here: the inlined fragment
    has no pagination of its own to drive."""
    replies = ids.get("_replies_by_parent", {}).get(parent_comment_id, [])
    for reply in replies[:max_replies]:
        yield reply
