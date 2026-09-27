"""TikTok comment crawler for the Hanoi-flood-impact-on-commuting topic --
top-N-by-likes per video via network-response interception.

A real browser (Patchright/Playwright) loads each video page; TikTok's own
JavaScript issues correctly-signed `/api/comment/list/` requests as it
renders and scrolls the comment panel. A network-response listener attached
BEFORE navigation reads the resulting JSON -- no request signing is
reproduced by this code. See README.md for the full design and workflow.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from crawlers.common.records import (  # noqa: E402
    JsonlWriter,
    batch_id,
    iso_utc_from_epoch,
    now_hcm,
    record_id,
    require_env_salt,
    salted_hash,
)
from crawlers.topics import is_on_topic_tiktok, matched_groups  # noqa: E402

try:
    # Patchright carries anti-detection patches over stock Playwright (plan §9).
    from patchright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - fallback path, exercised on machines without patchright
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - stdlib-only unit tests must still import this module
        sync_playwright = None

ROOT = "https://www.tiktok.com"
OUTPUT_DIR = "data/outputs/tiktok"
SPIKE_DIR = os.path.join(OUTPUT_DIR, "spike")
USER_DATA_DIR = os.path.join(OUTPUT_DIR, ".browser_profile")

# JSON keys observed live against TikTok's web app. Parsing below is
# defensive (try/except per comment) so an unexpected key never crashes a
# whole video.
COMMENT_API_MARKER = "/api/comment/list/"
REPLY_API_MARKER = "/api/comment/list/reply/"
SEARCH_API_MARKER = "/api/search/"

CAPTCHA_SELECTORS = [
    ".captcha_verify_container",
    "#captcha_container",
    "div[class*='captcha']",
]
COMMENT_PANEL_SELECTORS = [
    "[data-e2e='comment-list']",
    "[data-e2e='browse-comment-list']",
    "[data-e2e='comment-level-1']",
]
LOGIN_WALL_MARKERS = ("/login", "/passport/web/")
SOFT_BLOCK_STATUS_FLOOR = 10000

SOURCES_VERSION = "tiktok_flood_sources_v1_2026-09-24"

DEFAULTS = {
    "top_n": 0,  # 0 = keep the whole pool, no top-N cut (see select_top_n)
    "pool_size": 100000,  # effectively uncapped -- has_more_false/stale_rounds stop first
    "max_rounds": 200,  # raised so long comment threads aren't cut off before has_more_false
    "stale_rounds": 5,
    "delay_min": 15,
    "delay_max": 45,
    "max_videos": 50,
    "captcha_pause": 30,
}


class BlockedError(Exception):
    """CAPTCHA or soft block detected. Caller must stop the whole run (plan §12)."""


class SessionExpiredError(Exception):
    """Redirected to login. Caller must stop the run and re-run the cookie script."""


# --------------------------------------------------------------------------
# URL / id helpers
# --------------------------------------------------------------------------

_VIDEO_ID_PATTERN = re.compile(r"/video/(\d+)")


def canonical_video_url(url):
    """Strip tracking query params, keep the canonical /@handle/video/<id> path."""
    parsed = urllib.parse.urlparse(url)
    path = re.sub(r"/+$", "", parsed.path) or "/"
    return urllib.parse.urlunparse(("https", "www.tiktok.com", path, "", "", ""))


def video_id_from_url(url):
    match = _VIDEO_ID_PATTERN.search(url)
    return match.group(1) if match else ""


def is_login_wall(url):
    return any(marker in url for marker in LOGIN_WALL_MARKERS)


# --------------------------------------------------------------------------
# Pure functions — comment parsing, ranking, record mapping
# --------------------------------------------------------------------------


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_comment_page(payload):
    """One /api/comment/list/ JSON response -> (comments, has_more, cursor, status_code)."""
    if not isinstance(payload, dict):
        return [], False, None, None
    status_code = payload.get("status_code")
    has_more = bool(payload.get("has_more"))
    cursor = payload.get("cursor")
    comments = []
    for item in payload.get("comments") or []:
        try:
            cid = str(item["cid"])
            text = item.get("text", "")
        except (KeyError, TypeError):
            continue
        user = item.get("user") or {}
        comments.append({
            "cid": cid,
            "comment_text": text,
            "create_time": _safe_int(item.get("create_time")),
            "likes_count": _safe_int(item.get("digg_count")),
            "reply_count": _safe_int(item.get("reply_comment_total")),
            "unique_id": user.get("unique_id", ""),
            "reply_id": str(item.get("reply_id") or ""),
            "raw": item,
        })
    return comments, has_more, cursor, status_code


def is_soft_block(status_code, comments, has_more):
    """JSON-level soft block: looks like HTTP 200 success but is not (plan §12)."""
    if status_code is not None and _safe_int(status_code) >= SOFT_BLOCK_STATUS_FLOOR:
        return True
    if not comments and has_more:
        return True
    return False


def dedup_pool(existing, new_comments):
    """Merge new_comments into existing (keyed by cid), return (merged, added_count)."""
    merged = dict(existing)
    added = 0
    for comment in new_comments:
        if comment["cid"] not in merged:
            merged[comment["cid"]] = comment
            added += 1
    return merged, added


def select_top_n(pool, top_n):
    """Sort a comment pool by likes desc, tie-break, keep top_n."""
    ordered = sorted(
        pool,
        key=lambda c: (-c["likes_count"], c["create_time"], c["cid"]),
    )
    selected = ordered if top_n is None or top_n <= 0 else ordered[:top_n]
    return selected


def sampling_rule_for(top_n, pool_size):
    label = "all" if top_n is None or top_n <= 0 else f"top{top_n}"
    return f"{label}_by_likes_pool{pool_size}"


def comment_record_id(video_id, cid):
    return record_id("tiktok_flood_c", video_id, cid)


def to_record(
    *,
    video_url,
    video_id,
    post_context,
    video_stats,
    comment,
    like_rank,
    pool_size,
    top_n,
    batch_id_value,
    handle_salt,
    is_reply=False,
    parent_id=None,
    capture_method="api_intercept",
    discovery_query="",
    discovery_url="",
    source_kind="",
    source_class="",
    source_label="",
    hashtags=(),
    topic_rule="",
    matched_groups_=(),
    video_posted_at="",
):
    return {
        "id": comment_record_id(video_id, comment["cid"]),
        "platform": "tiktok",
        "source_url": video_url,
        "post_context": post_context,
        "video_posted_at": video_posted_at,
        "comment_text": comment["comment_text"],
        "posted_at_raw": iso_utc_from_epoch(comment["create_time"]),
        "likes_count": comment["likes_count"],
        "crawled_at": now_hcm().isoformat(),
        "crawl_batch_id": batch_id_value,
        "like_rank": like_rank,
        "pool_size": pool_size,
        "sampling_rule": sampling_rule_for(top_n, pool_size),
        "author_hash": salted_hash(comment["unique_id"], handle_salt),
        "is_reply": is_reply,
        "parent_id": parent_id,
        "reply_count": comment["reply_count"],
        "video_id": video_id,
        "video_stats": video_stats,
        "capture_method": capture_method,
        "source_kind": source_kind,
        "source_class": source_class,
        "source_label": source_label,
        "discovery_query": discovery_query,
        "discovery_url": discovery_url,
        "sources_version": SOURCES_VERSION,
        "hashtags": list(hashtags),
        "topic_rule": topic_rule,
        "matched_groups": list(matched_groups_),
    }


# --------------------------------------------------------------------------
# Video-page metadata (caption, stats) — parsed from rendered HTML
# --------------------------------------------------------------------------

_CAPTION_META_PATTERN = re.compile(
    r'<meta\s+name="description"\s+content="([^"]*)"', re.IGNORECASE
)
_CREATE_TIME_PATTERN = re.compile(r'"createTime"\s*:\s*"?(\d+)"?')
_STATS_PATTERNS = {
    "play_count": re.compile(r'"playCount"\s*:\s*"?(\d+)"?'),
    "digg_count": re.compile(r'"diggCount"\s*:\s*"?(\d+)"?'),
    "comment_count": re.compile(r'"commentCount"\s*:\s*"?(\d+)"?'),
    "share_count": re.compile(r'"shareCount"\s*:\s*"?(\d+)"?'),
}
_HASHTAG_PATTERN = re.compile(r"#(\w+)", re.UNICODE)


def extract_hashtags(caption):
    """Hashtags embedded in a caption/description, e.g. ``#ngaphanoi``."""
    return [f"#{tag}" for tag in _HASHTAG_PATTERN.findall(caption or "")]


def extract_video_meta(html, url):
    """Best-effort caption + engagement stats from a rendered video page."""
    caption_match = _CAPTION_META_PATTERN.search(html or "")
    post_context = caption_match.group(1) if caption_match else ""
    stats = {name: _safe_int(m.group(1)) if (m := pattern.search(html or "")) else 0
              for name, pattern in _STATS_PATTERNS.items()}
    create_time_match = _CREATE_TIME_PATTERN.search(html or "")
    video_posted_at = (
        iso_utc_from_epoch(_safe_int(create_time_match.group(1))) if create_time_match else ""
    )
    return {
        "post_context": post_context,
        "video_id": video_id_from_url(url),
        "video_stats": stats,
        "hashtags": extract_hashtags(post_context),
        "video_posted_at": video_posted_at,
    }


# --------------------------------------------------------------------------
# Browser plumbing — network interception, persistent context, scrolling
# --------------------------------------------------------------------------


class CommentCollector:
    """Attached to page.on("response") BEFORE navigation so the first page of
    comments (loaded during navigation) is never missed (plan §9)."""

    def __init__(self):
        self._comment_pages = []
        self._reply_pages = []

    def reset(self):
        self._comment_pages = []
        self._reply_pages = []

    def attach(self, page):
        page.on("response", self._on_response)

    def _on_response(self, response):
        url = response.url
        if COMMENT_API_MARKER not in url and SEARCH_API_MARKER not in url:
            return
        try:
            payload = response.json()
        except Exception:
            return
        if REPLY_API_MARKER in url:
            self._reply_pages.append((url, payload))
        elif COMMENT_API_MARKER in url:
            self._comment_pages.append((url, payload))

    def drain_comment_pages(self):
        pages, self._comment_pages = self._comment_pages, []
        return pages

    def drain_reply_pages(self):
        pages, self._reply_pages = self._reply_pages, []
        return pages


def warmup_session(page, wait_ms=3000):
    """Visit tiktok.com's homepage and scroll briefly before the run jumps
    into specific video URLs -- see crawl_tiktok.py's warmup_session for the
    full rationale (topic-agnostic mitigation, kept identical here)."""
    try:
        page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)
        page.mouse.move(640, 450)
        page.mouse.wheel(0, 400)
        page.wait_for_timeout(800)
    except Exception as exc:
        print(f"[WARMUP] failed, continuing anyway: {exc!r}")


@contextmanager
def launch_context(headless=False, user_data_dir=None):
    """Yield (page, collector). Caller drives navigation per video; the
    context (cookies, fingerprint) persists across the whole run (plan §9, §12)."""
    if sync_playwright is None:
        raise RuntimeError(
            "Missing dependency: patchright/playwright. Run "
            "`pip install -r requirements.txt` then `patchright install chromium`."
        )
    user_data_dir = user_data_dir or USER_DATA_DIR
    os.makedirs(user_data_dir, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir,
            headless=headless,
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            collector = CommentCollector()
            collector.attach(page)
            yield page, collector
        finally:
            context.close()


_LOCATE_COMMENT_SCROLL_TARGET_JS = """
() => {
    const candidates = [...document.querySelectorAll('div')].filter(el => {
        const s = getComputedStyle(el);
        return (s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 20;
    });
    if (!candidates.length) return null;
    const best = candidates.filter(el => el.getBoundingClientRect().left > window.innerWidth * 0.3)
                           .sort((a, b) => b.scrollHeight - a.scrollHeight)[0] || candidates[0];
    const r = best.getBoundingClientRect();
    return {x: r.left + r.width / 2, y: r.top + r.height / 2};
}
"""


def scroll_comment_panel(page, step=400, pause_ms=250, ticks=6):
    """Scroll the comment container in small steps."""
    try:
        point = page.evaluate(_LOCATE_COMMENT_SCROLL_TARGET_JS)
    except Exception:
        point = None
    if point:
        page.mouse.move(point["x"], point["y"])
    for _ in range(ticks):
        page.mouse.wheel(0, step)
        page.wait_for_timeout(pause_ms)


_OPEN_COMMENT_PANEL_JS = """
() => {
    const candidates = [
        ...document.querySelectorAll('[data-e2e*="comment-icon"], [data-e2e*="comment"]'),
        ...document.querySelectorAll('[role="button"], button'),
    ];
    for (const el of candidates) {
        const e2e = el.getAttribute('data-e2e') || '';
        const aria = el.getAttribute('aria-label') || '';
        if (e2e.includes('comment-icon') || /comment|b.nh lu.n/i.test(aria)) {
            try {
                el.click();
                return e2e || aria || 'matched-no-label';
            } catch (e) {
                continue;
            }
        }
    }
    return null;
}
"""


def open_comment_panel(page):
    """Best-effort click to expand the comment panel."""
    try:
        page.wait_for_selector('[data-e2e="comment-icon"]', timeout=10000)
    except Exception:
        pass
    try:
        return page.evaluate(_OPEN_COMMENT_PANEL_JS)
    except Exception:
        return None


def check_block_markers(page):
    """Raise BlockedError / SessionExpiredError if the page shows a known
    block signal (plan §12)."""
    if is_login_wall(page.url):
        raise SessionExpiredError(f"Redirected to login: {page.url}")
    for selector in CAPTCHA_SELECTORS:
        if page.query_selector(selector):
            raise BlockedError(f"CAPTCHA marker matched: {selector} on {page.url}")


# --------------------------------------------------------------------------
# Pipeline — per-video pool build, rank/select, replies
# --------------------------------------------------------------------------


_FIND_REPLY_EXPAND_BUTTON_JS = """
    (perThreadCap) => {
        const matches = (el) => {
            const label = (el.textContent || '').trim().toLowerCase();
            return label.startsWith('view') && (label.includes('repl') || label.includes('more'));
        };
        const containers = [...document.querySelectorAll(
            'div[class*="DivViewRepliesContainer"]:not([data-reply-cap-hit])'
        )];
        for (const c of containers) {
            let target = null;
            for (const el of c.querySelectorAll('button, [role="button"]')) {
                if (matches(el)) { target = el; break; }
            }
            if (!target) {
                const candidates = [...c.querySelectorAll('div, span')].filter(matches);
                target = candidates.length ? candidates[candidates.length - 1] : null;
            }
            if (!target) continue;
            const clicks = parseInt(c.getAttribute('data-reply-clicks') || '0', 10);
            if (clicks >= perThreadCap) {
                c.setAttribute('data-reply-cap-hit', '1');
                continue;
            }
            c.setAttribute('data-reply-clicks', String(clicks + 1));
            return target;
        }
        return null;
    }
"""


def build_pool(page, collector, url, pool_size, max_rounds, stale_rounds,
                *, with_replies=False, max_wait_ms=4000, max_reply_clicks=400,
                per_thread_click_cap=20, clicks_per_round=15):
    """Paginate the comment panel until pool_size reached, has_more is false,
    max_rounds is hit, or stale_rounds pass with no new comments (plan §3, §11).

    Reply-expand buttons are clicked INTERLEAVED into the same scroll loop
    when with_replies is set -- see crawl_tiktok.py's build_pool docstring
    for the full virtualization rationale (identical mechanics, reused here).

    Returns (pool_dict keyed by cid, replies list of (parent_cid, reply) dict
    tuples, stopped_reason).
    """
    pool = {}
    replies = []
    seen_reply_cids = set()
    total_clicks = 0
    stale = 0

    def _click_some_replies(budget):
        nonlocal total_clicks
        clicked = 0
        while clicked < budget and total_clicks < max_reply_clicks:
            collector.drain_reply_pages()
            button = page.evaluate_handle(_FIND_REPLY_EXPAND_BUTTON_JS, per_thread_click_cap).as_element()
            if button is None:
                break
            try:
                button.click()
            except Exception:
                break
            page.wait_for_timeout(max_wait_ms)
            for _, payload in collector.drain_reply_pages():
                reply_comments, _has_more, _cursor, status_code = parse_comment_page(payload)
                if is_soft_block(status_code, reply_comments, False):
                    continue
                for reply in reply_comments:
                    if reply["cid"] in seen_reply_cids:
                        continue
                    seen_reply_cids.add(reply["cid"])
                    parent_cid = reply.get("reply_id") or ""
                    if parent_cid in pool:
                        replies.append((parent_cid, reply))
            clicked += 1
            total_clicks += 1

    def _finish_replies():
        if with_replies:
            _click_some_replies(max_reply_clicks - total_clicks)

    for round_index in range(max_rounds):
        scroll_comment_panel(page)
        check_block_markers(page)
        pages = collector.drain_comment_pages()
        added_this_round = 0
        stop_has_more_false = False
        for _, payload in pages:
            comments, has_more, _cursor, status_code = parse_comment_page(payload)
            if is_soft_block(status_code, comments, has_more):
                raise BlockedError(f"Soft block on {url}: status_code={status_code} has_more={has_more}")
            pool, added = dedup_pool(pool, comments)
            added_this_round += added
            if not has_more:
                stop_has_more_false = True

        if with_replies:
            _click_some_replies(clicks_per_round)

        if len(pool) >= pool_size:
            _finish_replies()
            return pool, replies, "pool_target_reached"
        if stop_has_more_false:
            _finish_replies()
            return pool, replies, "has_more_false"
        if added_this_round == 0:
            stale += 1
            if stale >= stale_rounds:
                _finish_replies()
                return pool, replies, "stale_rounds"
        else:
            stale = 0
    _finish_replies()
    return pool, replies, "max_rounds"


def crawl_video(
    page,
    collector,
    video_url,
    *,
    top_n,
    pool_size,
    max_rounds,
    stale_rounds,
    with_replies,
    batch_id_value,
    handle_salt,
    source=None,
):
    """Full per-video pipeline: navigate, build pool, rank, select, map to
    records. Returns (records, pool_size_actual, stop_reason)."""
    source = source or {}
    canonical_url = canonical_video_url(video_url)
    video_id = video_id_from_url(canonical_url)
    collector.reset()
    page.goto(canonical_url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(3000)
    check_block_markers(page)

    open_comment_panel(page)
    page.wait_for_timeout(1500)
    meta = extract_video_meta(page.content(), page.url)

    pool_dict, fetched_replies, stop_reason = build_pool(
        page, collector, canonical_url, pool_size, max_rounds, stale_rounds, with_replies=with_replies,
    )
    pool = list(pool_dict.values())
    if not pool:
        return [], 0, stop_reason

    # Gate already ran at discovery time -- this is a defensive re-check
    # only, not a second gate (same convention as crawl_tiktok.py).
    topic_verdict, topic_rule = is_on_topic_tiktok(meta["post_context"], meta["hashtags"], source=source)
    if topic_verdict != "accept":
        return [], len(pool), f"topic_reject:{topic_rule}"

    selected = select_top_n(pool, top_n)
    selected_cids = {c["cid"] for c in selected}
    fetched_replies = [(pcid, r) for pcid, r in fetched_replies if pcid in selected_cids]

    records = []
    for rank, comment in enumerate(selected, start=1):
        records.append(to_record(
            video_url=canonical_url,
            video_id=video_id,
            post_context=meta["post_context"],
            video_stats=meta["video_stats"],
            video_posted_at=meta["video_posted_at"],
            comment=comment,
            like_rank=rank,
            pool_size=len(pool),
            top_n=top_n,
            batch_id_value=batch_id_value,
            handle_salt=handle_salt,
            source_kind=source.get("kind", ""),
            source_class=source.get("source_class", ""),
            source_label=source.get("label", ""),
            discovery_query=source.get("query", ""),
            discovery_url=source.get("url", ""),
            hashtags=meta["hashtags"],
            topic_rule=topic_rule,
            matched_groups_=matched_groups(comment["comment_text"]),
        ))

    if with_replies:
        for parent_cid, reply in fetched_replies:
            parent_record_id = comment_record_id(video_id, parent_cid)
            records.append(to_record(
                video_url=canonical_url,
                video_id=video_id,
                post_context=meta["post_context"],
                video_stats=meta["video_stats"],
                video_posted_at=meta["video_posted_at"],
                comment=reply,
                like_rank=0,
                pool_size=len(pool),
                top_n=top_n,
                batch_id_value=batch_id_value,
                handle_salt=handle_salt,
                is_reply=True,
                parent_id=parent_record_id,
                source_kind=source.get("kind", ""),
                source_class=source.get("source_class", ""),
                source_label=source.get("label", ""),
                discovery_query=source.get("query", ""),
                discovery_url=source.get("url", ""),
                hashtags=meta["hashtags"],
                topic_rule=topic_rule,
                matched_groups_=matched_groups(reply["comment_text"]),
            ))

    return records, len(pool), stop_reason


# --------------------------------------------------------------------------
# Discovery — URL file
# --------------------------------------------------------------------------


def read_url_file(path):
    # Windows PowerShell 5.1's `>` redirect writes UTF-16LE (with a FF FE
    # BOM); plain UTF-8 (sometimes EF BB BF BOM) otherwise -- sniff the
    # actual BOM bytes.
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xff\xfe"):
        text = raw.decode("utf-16-le")
    elif raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16-be")
    else:
        text = raw.decode("utf-8-sig")
    text = text.lstrip("﻿")
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _positive_int(value):
    ivalue = int(value)
    if ivalue < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return ivalue


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url-file", required=True,
                         help="Newline-delimited video URLs. Keyword discovery lives in "
                              "discover_tiktok.py, not here.")
    parser.add_argument("--top-n", type=_positive_int, default=DEFAULTS["top_n"])
    parser.add_argument("--pool-size", type=_positive_int, default=DEFAULTS["pool_size"])
    parser.add_argument("--with-replies", dest="with_replies", action="store_true", default=True,
                         help="Fetch replies for selected comments. On by default.")
    parser.add_argument("--no-replies", dest="with_replies", action="store_false",
                         help="Skip reply-fetching, only collect top-level comments.")
    parser.add_argument("--max-rounds", type=_positive_int, default=DEFAULTS["max_rounds"])
    parser.add_argument("--stale-rounds", type=_positive_int, default=DEFAULTS["stale_rounds"])
    parser.add_argument("--delay-min", type=_positive_int, default=DEFAULTS["delay_min"])
    parser.add_argument("--delay-max", type=_positive_int, default=DEFAULTS["delay_max"])
    parser.add_argument("--headful", dest="headless", action="store_false", default=None)
    parser.add_argument("--headless", dest="headless", action="store_true")
    parser.add_argument("--max-videos", type=_positive_int, default=DEFAULTS["max_videos"])
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--batch-id")
    parser.add_argument("--user-data-dir")
    parser.add_argument("--captcha-pause", type=int, default=DEFAULTS["captcha_pause"],
                         help="Seconds to pause after opening the first video, headful only, "
                              "so a CAPTCHA can be solved manually before the run proceeds. "
                              "0 disables the pause.")
    parser.add_argument("--warmup", dest="warmup", action="store_true", default=True,
                         help="Visit tiktok.com's homepage before jumping into video URLs. "
                              "On by default.")
    parser.add_argument("--no-warmup", dest="warmup", action="store_false")
    return parser


def main(argv=None):
    import random

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    handle_salt = require_env_salt("TIKTOK_HANDLE_SALT")

    urls = read_url_file(args.url_file)[: args.max_videos]
    if not urls:
        print("[STOP] no URLs to crawl.")
        return

    headless = bool(args.headless)  # None -> False (headful default)
    batch = args.batch_id or batch_id("tiktok_flood", now_hcm())
    out_path = os.path.join(args.output_dir, f"{batch}.jsonl")

    stats = {"attempted": 0, "succeeded": 0, "skipped_no_comments": 0, "skipped_off_topic": 0, "skipped_error": 0}
    pool_sizes = []
    stop_reason_summary = "completed"

    with launch_context(headless=headless, user_data_dir=args.user_data_dir) as (page, collector), \
         JsonlWriter(out_path) as writer:
        if args.warmup:
            print("\n[WARMUP] Visiting tiktok.com before jumping into video URLs...")
            warmup_session(page)

        if args.captcha_pause > 0 and not headless:
            print(f"\n[PAUSE] Waiting {args.captcha_pause}s before starting -- "
                  f"solve any CAPTCHA in the browser window now if one appears.")
            page.goto(canonical_video_url(urls[0]), wait_until="domcontentloaded", timeout=90000)
            time.sleep(args.captcha_pause)

        for index, url in enumerate(urls):
            if index > 0:
                delay = random.uniform(args.delay_min, args.delay_max)
                print(f"\nWait {delay:.1f}s before next video...")
                time.sleep(delay)

            print(f"\n=== [{index + 1}/{len(urls)}] {url} ===")
            stats["attempted"] += 1
            try:
                records, pool_size, video_stop_reason = crawl_video(
                    page, collector, url,
                    top_n=args.top_n,
                    pool_size=args.pool_size,
                    max_rounds=args.max_rounds,
                    stale_rounds=args.stale_rounds,
                    with_replies=args.with_replies,
                    batch_id_value=batch,
                    handle_salt=handle_salt,
                )
            except (BlockedError, SessionExpiredError) as exc:
                print(f"[STOP] {type(exc).__name__}: {exc}")
                print(f"[STOP] Videos completed before stop: {stats['succeeded']}/{len(urls)}")
                print(f"[STOP] Resume hint: re-run with the same --url-file, skipping the first {index} URLs.")
                stop_reason_summary = type(exc).__name__
                break
            except Exception as exc:
                print(f"[SKIP] video={url} error={exc!r}")
                stats["skipped_error"] += 1
                continue

            if not records:
                if video_stop_reason.startswith("topic_reject:"):
                    print(f"  [TOPIC SKIP] {video_stop_reason}")
                    stats["skipped_off_topic"] += 1
                else:
                    print(f"  no comments (stop_reason={video_stop_reason})")
                    stats["skipped_no_comments"] += 1
                continue

            pool_sizes.append(pool_size)
            written = sum(1 for r in records if writer.write(r))
            stats["succeeded"] += 1
            print(f"  wrote {written} records (pool_size={pool_size}, stop_reason={video_stop_reason})")

        mean_pool = sum(pool_sizes) / len(pool_sizes) if pool_sizes else 0
        min_pool = min(pool_sizes) if pool_sizes else 0
        print("\n=== Run summary ===")
        print(f"videos attempted / succeeded / skipped_no_comments / skipped_off_topic / skipped_error: "
              f"{stats['attempted']} / {stats['succeeded']} / {stats['skipped_no_comments']} / "
              f"{stats['skipped_off_topic']} / {stats['skipped_error']}")
        print(f"comments written: {writer.count}")
        print(f"pool_size mean / min: {mean_pool:.1f} / {min_pool}")
        print(f"stop reason: {stop_reason_summary}")
        print(f"Output: {out_path}")


if __name__ == "__main__":
    main()
