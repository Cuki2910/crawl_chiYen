"""Shared library for the newspaper comment crawler -- outlet-agnostic:
HTTP helpers, config, gate wiring, resumability, and the record schema.
Outlet-specific extraction/fetching lives in crawlers/news/adapters/ instead
(docs/plans/newspaper_plan.md §2, §11) -- this file must never grow
VnExpress-specific (or any other outlet's) logic again.

Split into discover_news.py (search -> gate -> candidate CSV) and
crawl_news.py (--url-file -> fetch comments -> write JSONL), the same
two-step shape as the TikTok crawler (docs/plans/newspaper_plan.md §6,
mirroring crawlers/tiktok/discover_tiktok.py + crawl_tiktok.py) so a human
reviews the discovery CSV before anything is crawled.

This crawler collects reader COMMENTS, not article text (plan §1). Article
body is fetched by an adapter and used to run the topic gate (plan §5,
revised 2026-07-29), then discarded -- never stored on an output record.
Only the <title> is kept, as lightweight context (post_context).
"""

import http.client
import json
import os
import re
import urllib.parse
import urllib.request

from crawlers.common.records import iso_utc_from_epoch, now_hcm, record_id, salted_hash

OUTPUT_DIR = "data/outputs/news"

OUTLETS = {
    "vnexpress": {
        "publisher_name": "VnExpress",
        "search_url_template": "https://timkiem.vnexpress.net/?q={query}",
        "article_url_pattern": re.compile(r"^https://vnexpress\.net/[a-z0-9-]+-\d{7,}\.html$"),
        "comment_api": "https://usi-saas.vnexpress.net/index/get",
        # Confirmed live 2026-07-31 via a browser spike clicking a real
        # "N tra loi" reply-expand link and capturing the network request --
        # not guessed. Reply items carry parent_id = the parent's own
        # comment_id (top-level items are self-parented: parent_id ==
        # comment_id), and the reply count to paginate against lives on the
        # parent item's own "replys": {"total": N} field, not in this
        # endpoint's response.
        "reply_api": "https://usi-saas.vnexpress.net/index/getreplay",
        "source_class": "policy_search",
    },
    "thanhnien": {
        "publisher_name": "Thanh Nien",
        # Confirmed live 2026-08-09: the param is `keywords`, not `q` --
        # /tim-kiem.htm?q=... 200s but silently ignores the query and
        # returns the homepage feed instead of search results.
        "search_url_template": "https://thanhnien.vn/tim-kiem.htm?keywords={query}",
        "article_url_pattern": re.compile(r"^https://thanhnien\.vn/[a-z0-9-]+-\d{15,19}\.htm$"),
        # CNND platform (shared comment infra, not VnExpress's usi-saas) --
        # confirmed live 2026-08-09 (plan Findings, Thanh Nien section).
        # HTML-fragment API, not JSON; replies are inlined in this same
        # response rather than a separate endpoint (see adapters/thanhnien.py).
        "comment_api": "https://eth2.cnnd.vn/api/ajax/ListComment.htm",
        "site_name": "thanhnien",
        "source_class": "policy_search",
    },
    "vietnamnet": {
        "publisher_name": "VietnamNet",
        # Search (/tim-kiem?q=) is bot-protected -- confirmed live
        # 2026-08-09: a real browser gets genuine query-filtered results,
        # plain HTTP always gets served a generic fallback page regardless
        # of query (not a caching artifact -- retried with cache-busting
        # params, still failed). Article pages and the comment API are
        # both open/unprotected. Discovery uses these category RSS feeds
        # instead (all confirmed live, 200 + ~1000 items each) with
        # client-side keyword filtering -- see adapters/vietnamnet.py.
        "rss_categories": ["thoi-su", "giao-thong", "doi-song"],
        "article_url_pattern": re.compile(r"^https://vietnamnet\.vn/[a-z0-9-]+-\d+\.html$"),
        # Real JSON REST API (unlike Thanh Nien's HTML fragments) --
        # confirmed live 2026-08-09. Replies reuse this same endpoint via
        # a ParentId param, no separate reply-listing endpoint exists.
        "comment_api": "https://comment.vietnamnet.vn/api/Comment/Gets",
        "website_id": "000003",
        "source_class": "policy_search",
        "discovery_mode": "rss",
    },
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
}

DEFAULTS = {
    "max_per_keyword": 10,
    "max_comments_per_article": 500,
    "comment_page_size": 24,
    "max_replies_per_comment": 500,
    "delay_min": 0.8,
    "delay_max": 1.5,
}

SOURCES_VERSION = "news_sources_v4_2026-08-09"


class NewsAPIError(Exception):
    pass


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------


def fetch_text(url):
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except http.client.IncompleteRead as exc:
        # Not a subclass of URLError/OSError, so every caller's
        # `except (urllib.error.URLError, OSError)` misses it and the run
        # crashes -- surfaced by Thanh Nien's ~700KB article pages (vs.
        # VnExpress's small JSON payloads), where a mid-transfer network
        # hiccup truncating the chunked response is far more likely.
        raise OSError(f"incomplete read: {exc}") from exc


def fetch_json(url, params):
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except http.client.IncompleteRead as exc:
        raise OSError(f"incomplete read: {exc}") from exc


# --------------------------------------------------------------------------
# Record mapping -- shared across outlets. `comment` is a NORMALIZED dict
# from an adapter's fetch_comments() (crawlers/news/adapters/__init__.py):
# native_comment_id, content, create_time, likes_count, author_name,
# is_pinned. No outlet-specific field names here on purpose.
# --------------------------------------------------------------------------


def comment_record_id(article_url, comment_id):
    return record_id("news_c", article_url, comment_id)


def to_record(
    *,
    article_url,
    publisher,
    title,
    published_at,
    comment,
    batch_id_value,
    handle_salt,
    topic_rule,
    matched_groups_,
    discovery_query,
    source_class,
    is_reply=False,
    parent_native_comment_id=None,
    discovery_mode="search",
):
    return {
        "id": comment_record_id(article_url, comment["native_comment_id"]),
        "platform": "news",
        "source_url": article_url,
        "post_context": title,
        "article_published_at": published_at,
        "comment_text": comment["content"],
        "posted_at_raw": iso_utc_from_epoch(comment.get("create_time")),
        "likes_count": comment.get("likes_count", 0),
        "crawled_at": now_hcm().isoformat(),
        "crawl_batch_id": batch_id_value,
        "publisher": publisher,
        "native_comment_id": comment.get("native_comment_id"),
        "author_hash": salted_hash(comment.get("author_name", ""), handle_salt),
        "is_pin": comment.get("is_pinned", False),
        "is_reply": is_reply,
        "parent_native_comment_id": parent_native_comment_id,
        "capture_method": "json_api",
        "topic_rule": topic_rule,
        "matched_groups": list(matched_groups_),
        "discovery_query": discovery_query,
        # VietnamNet's search route is bot-protected (confirmed live
        # 2026-08-09), so its discovery mechanism is RSS-feed filtering, not
        # a live search index -- "search" would misrepresent that outlet's
        # records. Defaults to "search" so VnExpress/Thanh Nien (both true
        # keyword search) are unaffected.
        "discovery_mode": discovery_mode,
        "source_class": source_class,
        "sources_version": SOURCES_VERSION,
    }


# --------------------------------------------------------------------------
# Shared file helpers
# --------------------------------------------------------------------------


def read_url_file(path):
    """Newline-delimited article URLs, blank/`#`-comment lines ignored -- a
    human filters discovery_<batch>.csv down to `keep=1` rows and exports
    the article_url column to a file like this (typically
    data/inputs/news/urls.txt) before running crawl_news.py.

    Identical BOM-sniffing to crawlers/tiktok/crawl_tiktok.py's
    read_url_file: Windows PowerShell 5.1's `>` redirect writes UTF-16LE
    (FF FE BOM) when capturing the documented export one-liner's output,
    but plain UTF-8 (sometimes with an EF BB BF BOM) if the file is instead
    pasted by hand in an editor -- sniff the actual bytes rather than
    assume one encoding, so this works regardless of how the file was
    produced.
    """
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xff\xfe"):
        text = raw.decode("utf-16-le")
    elif raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16-be")
    else:
        text = raw.decode("utf-8-sig")  # handles both with and without a UTF-8 BOM
    # utf-16-le/-be (unlike utf-8-sig) don't auto-strip the BOM.
    text = text.lstrip("﻿")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def load_seen_urls(output_dir):
    """Rebuild resumability state from existing JSONL output (plan §9) --
    scanning is cheap at this project's scale and needs no separate state
    file that could drift out of sync with the data."""
    seen = set()
    if not os.path.isdir(output_dir):
        return seen
    for name in os.listdir(output_dir):
        if not name.startswith("news_") or not name.endswith(".jsonl"):
            continue
        path = os.path.join(output_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "source_url" in record:
                        seen.add(record["source_url"])
        except OSError:
            continue
    return seen
