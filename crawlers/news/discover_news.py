"""Newspaper comment discovery for the Hanoi-flood-impact-on-commuting
topic -- search all configured outlets (VnExpress, Thanh Nien, VietnamNet),
pre-gate, write a candidate CSV for human review before any comment is
crawled.

Every candidate goes through the shared gate (`is_on_topic_news` from
crawlers/common/topics.py) -- both `accept` and `reject` verdicts are
written to the CSV. The pre-gate has full article title+body to work with
(unlike TikTok's caption-only pre-gate), so it's more reliable.

--since/--until date-window filtering applies on each article's
published_at (every outlet's extract_meta() already returns it). A row
outside the window is still written to the CSV (for audit visibility) but
forced to keep="0" regardless of gate verdict, and counted separately in
the run summary. An article with a missing/unparseable published_at is
treated as outside the window (conservative default -- can't confirm the
date criterion, so it doesn't get auto-kept) -- overridable by hand in the
CSV like any other row.

**Important limitation**: none of the three outlets' search engines accept
a date-range query parameter, so running this script twice with different
--since/--until values returns the SAME candidate set both times -- only
the in_date_window/keep labeling differs. Getting more results from a
specific historical window means raising --max-per-keyword (searching
deeper into each keyword's results), not re-running with different dates.
See README.md for the full design and workflow.
"""

import argparse
import csv
import os
import sys
import time
import urllib.error
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from crawlers.common.records import batch_id, now_hcm  # noqa: E402
from crawlers.hanoi_flood_transport.tiktok_news_queries import KEYWORDS  # noqa: E402
from crawlers.topics import is_on_topic_news  # noqa: E402
from crawlers.news.adapters import ADAPTERS  # noqa: E402
from crawlers.news.news_common import DEFAULTS, OUTLETS, fetch_text  # noqa: E402

OUTPUT_DIR = "data/outputs/news"

CSV_FIELDS = ["article_url", "publisher", "discovery_query", "title", "published_at",
              "gate_verdict", "gate_rule", "in_date_window", "has_comments_confirmed", "keep"]

_KEEP_FOR_VERDICT = {"accept": "1", "reject": "0"}


def _parse_date_bound(value):
    """--since/--until: an ISO date or datetime. Naive values are assumed
    UTC -- same convention as discover_tiktok.py's --since/--until."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_published_at(value):
    """Article's published_at (ISO 8601 with an offset, e.g.
    "2026-07-30T15:11:00+07:00", per all three adapters' extract_meta()) ->
    an aware datetime, or None if missing/unparseable. Normalizes a naive
    result to UTC (same convention as _parse_date_bound) -- confirmed live
    an adapter can return a published_at with no offset for some articles,
    which otherwise crashes the since/until comparison below with
    "can't compare offset-naive and offset-aware datetimes" instead of
    just being treated as outside the window."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def in_date_window(published_at_str, since, until):
    """False (not just "unknown") when published_at can't be parsed -- see
    module docstring for why that's the conservative default here."""
    dt = _parse_published_at(published_at_str)
    if dt is None:
        return False
    if since is not None and dt < since:
        return False
    if until is not None and dt > until:
        return False
    return True


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", action="append", default=[],
                         help="Repeatable; defaults to the built-in flood KEYWORDS list.")
    parser.add_argument("--outlet", action="append", choices=sorted(OUTLETS), default=[],
                         help="Repeatable; restrict discovery to these outlet key(s). "
                              "Defaults to all configured outlets.")
    parser.add_argument("--since", type=_parse_date_bound, default=None,
                         help="ISO date/datetime; naive values assumed UTC. Run this script "
                              "twice for the two flood-topic windows, e.g. "
                              "--since 2025-08-01 --until 2025-10-31, then "
                              "--since 2026-08-01 (no --until = through now).")
    parser.add_argument("--until", type=_parse_date_bound, default=None)
    parser.add_argument("--max-per-keyword", type=int, default=DEFAULTS["max_per_keyword"])
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--batch-id")
    return parser


def main(argv=None):
    import random

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    keywords = args.keyword or KEYWORDS
    outlets = {k: OUTLETS[k] for k in args.outlet} if args.outlet else OUTLETS

    os.makedirs(args.output_dir, exist_ok=True)
    batch = args.batch_id or batch_id("news_flood", now_hcm())
    csv_path = os.path.join(args.output_dir, f"discovery_{batch}.csv")

    seen_this_run = set()
    stats = {"discovered": 0, "accept": 0, "reject": 0, "no_widget": 0,
              "outside_window": 0, "errored": 0}

    with open(csv_path, "w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for outlet_key, outlet in outlets.items():
            adapter = ADAPTERS[outlet_key]
            for keyword in keywords:
                print(f"\n=== {outlet['publisher_name']}: {keyword!r} ===")
                try:
                    urls = adapter.discover_urls(outlet, keyword, args.max_per_keyword)
                except (urllib.error.URLError, OSError) as exc:
                    print(f"  [SEARCH ERROR] {exc!r}")
                    continue

                for url in urls:
                    if url in seen_this_run:
                        continue
                    seen_this_run.add(url)
                    stats["discovered"] += 1

                    row = {"article_url": url, "publisher": outlet["publisher_name"],
                           "discovery_query": keyword, "title": "", "published_at": "",
                           "gate_verdict": "", "gate_rule": "", "in_date_window": "",
                           "has_comments_confirmed": "", "keep": "0"}
                    try:
                        html = fetch_text(url)
                    except (urllib.error.URLError, OSError) as exc:
                        print(f"  [FETCH ERROR] {url} {exc!r}")
                        stats["errored"] += 1
                        writer.writerow(row)
                        continue

                    title, body, published_at, ids = adapter.extract_meta(html)
                    row["title"] = title
                    row["published_at"] = published_at
                    row["has_comments_confirmed"] = "yes" if ids is not None else "no_widget"
                    if ids is None:
                        stats["no_widget"] += 1

                    window_ok = in_date_window(published_at, args.since, args.until)
                    row["in_date_window"] = "yes" if window_ok else "no"
                    if not window_ok:
                        stats["outside_window"] += 1

                    verdict, rule = is_on_topic_news(title, body=body, source=outlet)
                    row["gate_verdict"], row["gate_rule"] = verdict, rule
                    row["keep"] = _KEEP_FOR_VERDICT[verdict] if (ids is not None and window_ok) else "0"
                    stats[verdict] += 1
                    print(f"  {verdict:16s} window={window_ok!s:5s} {title[:60]!r}  ({url})")

                    writer.writerow(row)
                    time.sleep(random.uniform(DEFAULTS["delay_min"], DEFAULTS["delay_max"]))

    print("\n=== Discovery summary ===")
    print(f"discovered: {stats['discovered']}")
    print(f"accept / reject: {stats['accept']} / {stats['reject']}")
    print(f"outside_window (published_at outside --since/--until, or unparseable): {stats['outside_window']}")
    print(f"no_widget (comment section absent on this article): {stats['no_widget']}")
    print(f"errored: {stats['errored']}")
    print(f"Discovery CSV: {csv_path}")
    print("\nNext steps:")
    print("1. Open the CSV. Spot-check accept/reject rows, override `keep` as needed.")
    print("2. Export the kept URLs into a plain list:")
    print(f"   python -c \"import csv; rows = csv.DictReader(open('{csv_path}', encoding='utf-8')); "
          f"print('\\n'.join(r['article_url'] for r in rows if r['keep'] == '1'))\" "
          f"> data/inputs/news/urls.txt")
    print("3. Check data/inputs/news/urls.txt looks right, then:")
    print("   python crawlers/news/crawl_news.py --url-file data/inputs/news/urls.txt")


if __name__ == "__main__":
    main()
