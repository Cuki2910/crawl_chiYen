"""TikTok video discovery for the Hanoi-flood-impact-on-commuting topic --
keyword search only, pre-gated.

Separate script from crawl_tiktok.py on purpose: a CAPTCHA hit during
discovery must not destroy an in-progress comment crawl. Every candidate
goes through the shared gate (`is_on_topic_tiktok` from
crawlers/common/topics.py) -- both `accept` and `reject` verdicts are
written to the candidate CSV for human review. No hashtag surface yet --
topics.py has no TIKTOK_HASHTAGS list, so SOURCES here is keyword-search
only. See README.md for the full design and workflow.
"""

import argparse
import csv
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from crawlers.common.records import iso_utc_from_epoch, now_hcm  # noqa: E402
from crawlers.hanoi_flood_transport.tiktok_news_queries import KEYWORDS  # noqa: E402
from crawlers.topics import is_on_topic_tiktok  # noqa: E402
from crawlers.tiktok.crawl_tiktok import (  # noqa: E402
    ROOT,
    BlockedError,
    SessionExpiredError,
    canonical_video_url,
    check_block_markers,
    extract_hashtags,
    launch_context,
)

# Confirmed live against real TikTok traffic during discovery development.
SEARCH_ITEM_MARKER = "/api/search/item/full/"
HASHTAG_ITEM_MARKER = "/api/challenge/item_list/"

DISCOVERY_ROUNDS = 6
DELAY_BETWEEN_SOURCES = 20

OUTPUT_DIR = "data/outputs/tiktok"
SOURCES_VERSION = "tiktok_flood_sources_v1_2026-09-24"


def _slugify(text):
    """"ngập Nguyễn Trãi" -> "ngap_nguyen_trai" -- a readable, deterministic
    CSV label derived straight from the query text, rather than 100
    hand-typed labels (doesn't scale to this topic's keyword count).
    "đ"/"Đ" are replaced explicitly before NFKD-folding -- confirmed live
    they don't decompose to "d" + a combining mark under NFKD (unlike
    "á"/"ế"/etc.), so without this they silently vanish instead of folding
    (e.g. "đường" -> "uong", not "duong")."""
    text = text.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text


def _build_sources():
    """Slugified labels can collide for near-duplicate phrasings (e.g.
    "ngập Hà Nội" and "ngập Ha Noi" both fold to "ngap_ha_noi") -- confirmed
    live, one collision across the 100 flood keywords. Disambiguated with a
    numeric suffix on the second+ occurrence so every source has a unique,
    filterable label."""
    seen_labels = {}
    sources = []
    for query in KEYWORDS:
        base_label = f"kw_{_slugify(query)}"
        seen_labels[base_label] = seen_labels.get(base_label, 0) + 1
        label = base_label if seen_labels[base_label] == 1 else f"{base_label}_{seen_labels[base_label]}"
        sources.append({"kind": "keyword_search", "label": label, "source_class": "keyword_search", "query": query})
    return sources


SOURCES = _build_sources()

CSV_FIELDS = [
    "video_id", "url", "discovery_query", "source_label", "caption",
    "hashtags", "create_time", "comment_count", "gate_verdict", "gate_rule", "keep",
]


# --------------------------------------------------------------------------
# Pure functions -- endpoint-aware extraction, offline-testable
# (identical logic to discover_tiktok.py; duplicated per this branch's
# deliberate no-shared-code-between-topics convention)
# --------------------------------------------------------------------------


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _candidate_from_item(item, hashtags_from):
    """One /api/search/item/full/ or /api/challenge/item_list/ item -> a
    normalized candidate dict, or None if required fields are missing."""
    try:
        video_id = str(item["id"])
    except (KeyError, TypeError):
        return None
    author = item.get("author") or {}
    unique_id = author.get("uniqueId", "")
    if not unique_id:
        return None
    caption = item.get("desc", "") or ""

    if hashtags_from == "text_extra":
        hashtags = [f"#{h['hashtagName']}" for h in (item.get("textExtra") or []) if h.get("hashtagName")]
        if not hashtags:
            hashtags = extract_hashtags(caption)
    else:
        hashtags = extract_hashtags(caption)

    stats = item.get("stats") or {}
    return {
        "video_id": video_id,
        "url": canonical_video_url(f"{ROOT}/@{unique_id}/video/{video_id}"),
        "caption": caption,
        "hashtags": hashtags,
        "create_time": _safe_int(item.get("createTime")),
        "video_stats": {
            "play_count": _safe_int(stats.get("playCount")),
            "digg_count": _safe_int(stats.get("diggCount")),
            "comment_count": _safe_int(stats.get("commentCount")),
            "share_count": _safe_int(stats.get("shareCount")),
        },
    }


def parse_search_item_page(payload):
    """/api/search/item/full/ -> (candidates, has_more, cursor)."""
    if not isinstance(payload, dict):
        return [], False, None
    has_more = bool(payload.get("has_more"))
    cursor = payload.get("cursor")
    candidates = [
        c for item in (payload.get("item_list") or [])
        if (c := _candidate_from_item(item, hashtags_from="caption")) is not None
    ]
    return candidates, has_more, cursor


def parse_hashtag_item_page(payload):
    """/api/challenge/item_list/ -> (candidates, has_more, cursor). Unused
    while SOURCES is keyword-search-only, kept for when a flood hashtag
    list gets added later."""
    if not isinstance(payload, dict):
        return [], False, None
    has_more = bool(payload.get("hasMore"))
    cursor = payload.get("cursor")
    candidates = [
        c for item in (payload.get("itemList") or [])
        if (c := _candidate_from_item(item, hashtags_from="text_extra")) is not None
    ]
    return candidates, has_more, cursor


def dedup_candidates(existing, new_candidates):
    """Merge new_candidates into existing (keyed by video_id), return (merged, added_count)."""
    merged = dict(existing)
    added = 0
    for candidate in new_candidates:
        if candidate["video_id"] not in merged:
            merged[candidate["video_id"]] = candidate
            added += 1
    return merged, added


def in_date_window(create_time, since_epoch, until_epoch):
    if since_epoch is not None and create_time < since_epoch:
        return False
    if until_epoch is not None and create_time > until_epoch:
        return False
    return True


def keep_prefill(verdict):
    """CSV `keep` column prefill: accept -> "1", reject -> "0"."""
    return "1" if verdict == "accept" else "0"


# --------------------------------------------------------------------------
# Browser plumbing -- network interception for the two discovery surfaces
# --------------------------------------------------------------------------


class DiscoveryCollector:
    """Attached to page.on("response") before navigation, same rule as
    crawl_tiktok.py's CommentCollector -- the first page of results, loaded
    during navigation, must not be missed."""

    def __init__(self):
        self._search_pages = []
        self._hashtag_pages = []

    def reset(self):
        self._search_pages = []
        self._hashtag_pages = []

    def attach(self, page):
        page.on("response", self._on_response)

    def _on_response(self, response):
        url = response.url
        if SEARCH_ITEM_MARKER in url:
            try:
                self._search_pages.append(response.json())
            except Exception:
                pass
        elif HASHTAG_ITEM_MARKER in url:
            try:
                self._hashtag_pages.append(response.json())
            except Exception:
                pass

    def drain_search(self):
        pages, self._search_pages = self._search_pages, []
        return pages

    def drain_hashtag(self):
        pages, self._hashtag_pages = self._hashtag_pages, []
        return pages


def discover_source(page, collector, source, since_epoch, until_epoch, max_candidates, rounds=DISCOVERY_ROUNDS):
    """Navigate to one source's surface, paginate by scrolling, return a
    list of in-window candidate dicts capped at max_candidates."""
    kind = source["kind"]
    query = source["query"]
    if kind == "keyword_search":
        url = f"{ROOT}/search/video?q={urllib.parse.quote(query)}"
        parse_page, drain = parse_search_item_page, collector.drain_search
    elif kind == "hashtag":
        url = f"{ROOT}/tag/{urllib.parse.quote(query)}"
        parse_page, drain = parse_hashtag_item_page, collector.drain_hashtag
    else:
        raise ValueError(f"Unsupported source kind: {kind!r}")

    collector.reset()
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(4000)
    check_block_markers(page)

    candidates = {}
    for _round_index in range(rounds):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(2000)
        check_block_markers(page)
        for payload in drain():
            items, _has_more, _cursor = parse_page(payload)
            in_window = [c for c in items if in_date_window(c["create_time"], since_epoch, until_epoch)]
            candidates, _added = dedup_candidates(candidates, in_window)
        if len(candidates) >= max_candidates:
            break

    return list(candidates.values())[:max_candidates]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _positive_int(value):
    ivalue = int(value)
    if ivalue < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return ivalue


def _parse_date_bound(value):
    """--since/--until: an ISO date or datetime. Naive values are assumed
    UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _selected_sources(labels, classes):
    sources = SOURCES
    if labels:
        labels = set(labels)
        sources = [s for s in sources if s["label"] in labels]
    if classes:
        classes = set(classes)
        sources = [s for s in sources if s["source_class"] in classes]
    return sources


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-label", action="append", default=[])
    parser.add_argument("--source-class", action="append", default=[])
    parser.add_argument("--since", type=_parse_date_bound, default=None,
                         help="ISO date/datetime; naive values assumed UTC. Run this script "
                              "twice for the two flood-topic windows, e.g. "
                              "--since 2025-08-01 --until 2025-10-31, then "
                              "--since 2026-08-01 (no --until = through now).")
    parser.add_argument("--until", type=_parse_date_bound, default=None)
    parser.add_argument("--max-per-keyword", type=_positive_int, default=30)
    parser.add_argument("--max-candidates", type=_positive_int, default=2000)
    parser.add_argument("--headful", dest="headless", action="store_false", default=None)
    parser.add_argument("--headless", dest="headless", action="store_true")
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--batch-id")
    parser.add_argument("--user-data-dir")
    return parser


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    sources = _selected_sources(args.source_label, args.source_class)
    if not sources:
        print("[STOP] no sources match the given --source-label/--source-class filters.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    headless = bool(args.headless)
    batch = args.batch_id or f"tiktok_flood_discovery_{now_hcm().strftime('%Y%m%d_%H%M')}"
    out_path = os.path.join(args.output_dir, f"{batch}.csv")

    stats = {"discovered": 0, "accept": 0, "reject": 0}
    seen_videos = set()
    stop_reason = "completed"

    with launch_context(headless=headless, user_data_dir=args.user_data_dir) as (page, _unused_collector), \
         open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        collector = DiscoveryCollector()
        collector.attach(page)

        for index, source in enumerate(sources):
            if stats["discovered"] >= args.max_candidates:
                print(f"[STOP] --max-candidates {args.max_candidates} reached.")
                break
            if index > 0:
                print(f"\nWait {DELAY_BETWEEN_SOURCES}s before next source...")
                time.sleep(DELAY_BETWEEN_SOURCES)

            print(f"\n=== Source: {source['label']} ({source['kind']}: {source['query']!r}) ===")
            remaining = args.max_candidates - stats["discovered"]
            effective_max = min(args.max_per_keyword, remaining)
            try:
                candidates = discover_source(page, collector, source, args.since, args.until, effective_max)
            except (BlockedError, SessionExpiredError) as exc:
                print(f"[STOP] {type(exc).__name__}: {exc}")
                print(f"[STOP] Candidates written before stop: {stats['discovered']}")
                stop_reason = type(exc).__name__
                break
            except Exception as exc:
                print(f"[SOURCE ERROR] {source['label']}: {exc!r}")
                continue

            new_this_source = 0
            for candidate in candidates:
                if candidate["video_id"] in seen_videos:
                    continue
                seen_videos.add(candidate["video_id"])
                stats["discovered"] += 1
                new_this_source += 1

                verdict, rule = is_on_topic_tiktok(candidate["caption"], candidate["hashtags"], source=source)
                stats[verdict] = stats.get(verdict, 0) + 1

                writer.writerow({
                    "video_id": candidate["video_id"],
                    "url": candidate["url"],
                    "discovery_query": source.get("query", ""),
                    "source_label": source.get("label", ""),
                    "caption": candidate["caption"],
                    "hashtags": " ".join(candidate["hashtags"]),
                    "create_time": iso_utc_from_epoch(candidate["create_time"]),
                    "comment_count": candidate["video_stats"]["comment_count"],
                    "gate_verdict": verdict,
                    "gate_rule": rule,
                    "keep": keep_prefill(verdict),
                })
            f.flush()
            print(f"  {new_this_source} new candidates (accept/reject mix in CSV)")

    print("\n=== Discovery summary ===")
    print(f"candidates discovered: {stats['discovered']}")
    print(f"accept / reject: {stats.get('accept', 0)} / {stats.get('reject', 0)}")
    print(f"stop reason: {stop_reason}")
    print(f"Output: {out_path}")


if __name__ == "__main__":
    main()
