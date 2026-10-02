"""Threads post discovery for the Hanoi-flood-impact-on-commuting topic --
keyword search only, pre-gated. Same discover-then-review shape as
discover_tiktok.py: a candidate CSV with a `keep` column, human-reviewed
before crawl_threads.py ever runs.

Threads hides search results behind a login wall for logged-out browsers,
so this always runs against the persistent authenticated profile from
threads_session.py -- run that script's interactive_login() once first.

NOTE: Threads' search page is a client-rendered React app; the exact
in-page selectors used to read post cards below (POST_CARD_SELECTOR etc.)
are best-effort placeholders, not confirmed against live Threads markup.
Verify/adjust extract_candidates_from_page() against the real search page
before running this for real -- see README.md for how the TikTok/News
crawlers' equivalents were confirmed live before being trusted.
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
from crawlers.hanoi_flood_transport.electric_bus_queries import KEYWORDS as ELECTRIC_BUS_KEYWORDS  # noqa: E402
from crawlers.hanoi_flood_transport.tiktok_news_queries import KEYWORDS  # noqa: E402
from crawlers.topics import is_on_topic_threads, is_on_topic_threads_electric_bus  # noqa: E402
from crawlers.threads.threads_session import (  # noqa: E402
    ROOT,
    BlockedError,
    SessionExpiredError,
    check_block_markers,
    launch_context,
)

DISCOVERY_ROUNDS = 6
DELAY_BETWEEN_SOURCES = 20

OUTPUT_DIR = "data/outputs/threads"
SOURCES_VERSION = "threads_flood_sources_v1_2026-09-27"

_HASHTAG_PATTERN = re.compile(r"#(\w+)", re.UNICODE)


def extract_hashtags(text):
    return [f"#{tag}" for tag in _HASHTAG_PATTERN.findall(text or "")]


def _slugify(text):
    """Same convention as discover_tiktok.py's _slugify -- a readable,
    deterministic CSV label derived from the query text."""
    text = text.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text


def _build_sources(keywords):
    seen_labels = {}
    sources = []
    for query in keywords:
        base_label = f"kw_{_slugify(query)}"
        seen_labels[base_label] = seen_labels.get(base_label, 0) + 1
        label = base_label if seen_labels[base_label] == 1 else f"{base_label}_{seen_labels[base_label]}"
        sources.append({"kind": "keyword_search", "label": label, "source_class": "keyword_search", "query": query})
    return sources


CAMPAIGNS = {
    "flood_transport": {
        "sources": _build_sources(KEYWORDS),
        "gate": is_on_topic_threads,
        "batch_prefix": "threads_flood_discovery",
    },
    "electric_bus": {
        "sources": _build_sources(ELECTRIC_BUS_KEYWORDS),
        "gate": is_on_topic_threads_electric_bus,
        "batch_prefix": "threads_electric_bus_discovery",
    },
}

CSV_FIELDS = [
    "post_id", "url", "discovery_query", "source_label", "post_text",
    "hashtags", "posted_at", "reply_count", "gate_verdict", "gate_rule", "keep",
]


# --------------------------------------------------------------------------
# Pure functions -- offline-testable
# --------------------------------------------------------------------------


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def candidate_from_card(card):
    """One extracted search-result card dict -> a normalized candidate, or
    None if required fields are missing. ``card`` is whatever
    extract_candidates_from_page() hands back per result -- kept as a
    separate pure function so it's unit-testable without a browser."""
    post_id = card.get("post_id") or ""
    url = card.get("url") or ""
    if not post_id or not url:
        return None
    post_text = card.get("post_text", "") or ""
    hashtags = card.get("hashtags") or extract_hashtags(post_text)
    return {
        "post_id": post_id,
        "url": url,
        "post_text": post_text,
        "hashtags": hashtags,
        "posted_at_epoch": _safe_int(card.get("posted_at_epoch")),
        "reply_count": _safe_int(card.get("reply_count")),
    }


def dedup_candidates(existing, new_candidates):
    merged = dict(existing)
    added = 0
    for candidate in new_candidates:
        if candidate["post_id"] not in merged:
            merged[candidate["post_id"]] = candidate
            added += 1
    return merged, added


def in_date_window(posted_at_epoch, since_epoch, until_epoch):
    if since_epoch is not None and posted_at_epoch < since_epoch:
        return False
    if until_epoch is not None and posted_at_epoch > until_epoch:
        return False
    return True


def keep_prefill(verdict):
    return "1" if verdict == "accept" else "0"


# --------------------------------------------------------------------------
# Browser plumbing -- DOM extraction (placeholder selectors, see module docstring)
# --------------------------------------------------------------------------

_EXTRACT_SEARCH_CARDS_JS = """
() => {
    // Placeholder: Threads search-result cards, not confirmed against live
    // markup. Adjust the selector/attribute reads once verified.
    const cards = [...document.querySelectorAll('div[data-pressable-container] a[href*="/post/"]')];
    const seen = new Set();
    const results = [];
    for (const link of cards) {
        const href = link.getAttribute('href') || '';
        const match = href.match(/\\/post\\/([A-Za-z0-9_-]+)/);
        if (!match || seen.has(match[1])) continue;
        seen.add(match[1]);
        const container = link.closest('div[data-pressable-container]') || link;
        const text = container.innerText || '';
        const timeEl = container.querySelector('time');
        results.push({
            post_id: match[1],
            url: href.startsWith('http') ? href : (location.origin + href),
            post_text: text.slice(0, 2000),
            posted_at_iso: timeEl ? timeEl.getAttribute('datetime') : null,
        });
    }
    return results;
}
"""


def _iso_to_epoch(iso_string):
    if not iso_string:
        return 0
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except ValueError:
        return 0


def extract_candidates_from_page(page):
    """Read currently-rendered search-result cards off the page. Returns
    raw card dicts for candidate_from_card() to normalize."""
    raw_cards = page.evaluate(_EXTRACT_SEARCH_CARDS_JS)
    cards = []
    for raw in raw_cards:
        raw["posted_at_epoch"] = _iso_to_epoch(raw.get("posted_at_iso"))
        cards.append(raw)
    return cards


def discover_source(page, source, since_epoch, until_epoch, max_candidates, rounds=DISCOVERY_ROUNDS):
    query = source["query"]
    url = f"{ROOT}/search?q={urllib.parse.quote(query)}&serp_type=default"

    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(4000)
    check_block_markers(page)

    candidates = {}
    for _round_index in range(rounds):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(2000)
        check_block_markers(page)
        raw_cards = extract_candidates_from_page(page)
        normalized = [c for card in raw_cards if (c := candidate_from_card(card)) is not None]
        in_window = [c for c in normalized if in_date_window(c["posted_at_epoch"], since_epoch, until_epoch)]
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
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _selected_sources(sources, labels, classes):
    if labels:
        labels = set(labels)
        sources = [s for s in sources if s["label"] in labels]
    if classes:
        classes = set(classes)
        sources = [s for s in sources if s["source_class"] in classes]
    return sources


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", choices=tuple(CAMPAIGNS), default="flood_transport")
    parser.add_argument("--source-label", action="append", default=[])
    parser.add_argument("--source-class", action="append", default=[])
    parser.add_argument("--since", type=_parse_date_bound, default=None,
                         help="ISO date/datetime; naive values assumed UTC. Run twice for the "
                              "two flood-topic windows, e.g. --since 2025-08-01 --until "
                              "2025-10-31, then --since 2026-08-01 (no --until = through now).")
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

    campaign = CAMPAIGNS[args.campaign]
    gate = campaign["gate"]
    sources = _selected_sources(campaign["sources"], args.source_label, args.source_class)
    if not sources:
        print("[STOP] no sources match the given --source-label/--source-class filters.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    headless = bool(args.headless)
    batch = args.batch_id or f"{campaign['batch_prefix']}_{now_hcm().strftime('%Y%m%d_%H%M')}"
    out_path = os.path.join(args.output_dir, f"{batch}.csv")

    stats = {"discovered": 0, "accept": 0, "reject": 0}
    seen_posts = set()
    stop_reason = "completed"

    with launch_context(headless=headless, user_data_dir=args.user_data_dir) as (page, _context), \
         open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for index, source in enumerate(sources):
            if stats["discovered"] >= args.max_candidates:
                print(f"[STOP] --max-candidates {args.max_candidates} reached.")
                break
            if index > 0:
                print(f"\nWait {DELAY_BETWEEN_SOURCES}s before next source...")
                time.sleep(DELAY_BETWEEN_SOURCES)

            print(f"\n=== Source: {source['label']} (keyword_search: {source['query']!r}) ===")
            remaining = args.max_candidates - stats["discovered"]
            effective_max = min(args.max_per_keyword, remaining)
            try:
                candidates = discover_source(page, source, args.since, args.until, effective_max)
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
                if candidate["post_id"] in seen_posts:
                    continue
                seen_posts.add(candidate["post_id"])
                stats["discovered"] += 1
                new_this_source += 1

                verdict, rule = gate(candidate["post_text"], candidate["hashtags"], source=source)
                stats[verdict] = stats.get(verdict, 0) + 1

                writer.writerow({
                    "post_id": candidate["post_id"],
                    "url": candidate["url"],
                    "discovery_query": source.get("query", ""),
                    "source_label": source.get("label", ""),
                    "post_text": candidate["post_text"],
                    "hashtags": " ".join(candidate["hashtags"]),
                    "posted_at": iso_utc_from_epoch(candidate["posted_at_epoch"]),
                    "reply_count": candidate["reply_count"],
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
