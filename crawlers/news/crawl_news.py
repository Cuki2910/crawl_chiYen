"""Newspaper comment crawler for the Hanoi-flood-impact-on-commuting topic --
fetch comments for a reviewed list of article URLs, post-gate, write JSONL.

OUTLETS/ADAPTERS and the HTTP helpers (fetch_text, load_seen_urls,
read_url_file) come from news_common.py, which is pure outlet/platform
wiring (VnExpress/Thanh Nien/VietnamNet endpoint shapes) -- unrelated to
which topic is being crawled. news_common.to_record()'s ``id`` and
``sources_version`` fields ARE overridden per record below, so every record
gets this topic's own provenance markers rather than news_common's
defaults. See README.md for the full design and workflow.
"""

import argparse
import os
import random
import sys
import time
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from crawlers.common.records import JsonlWriter, batch_id, now_hcm, record_id, require_env_salt  # noqa: E402
from crawlers.topics import is_on_topic_news, matched_groups  # noqa: E402
from crawlers.news.adapters import ADAPTERS  # noqa: E402
from crawlers.news.news_common import (  # noqa: E402
    DEFAULTS,
    NewsAPIError,
    OUTLETS,
    fetch_text,
    load_seen_urls,
    read_url_file,
    to_record,
)

OUTPUT_DIR = "data/outputs/news"
SOURCES_VERSION = "news_flood_sources_v1_2026-09-24"


def comment_record_id(article_url, comment_id):
    return record_id("news_flood_c", article_url, comment_id)


def _resolve_outlet(url):
    """Match a URL back to its OUTLETS entry + adapter by article_url_pattern.
    None if no configured outlet claims it."""
    for outlet_key, outlet in OUTLETS.items():
        if outlet["article_url_pattern"].match(url):
            return outlet_key, outlet, ADAPTERS[outlet_key]
    return None, None, None


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url-file", required=True,
                         help="Newline-delimited article URLs (reviewed discovery output).")
    parser.add_argument("--max-comments-per-article", type=int,
                         default=DEFAULTS["max_comments_per_article"])
    parser.add_argument("--with-replies", dest="with_replies", action="store_true", default=True,
                         help="Also fetch each top-level comment's replies (records get "
                              "is_reply=True, parent_native_comment_id set). On by default.")
    parser.add_argument("--no-replies", dest="with_replies", action="store_false",
                         help="Skip reply-fetching, only collect top-level comments.")
    parser.add_argument("--max-replies-per-comment", type=int,
                         default=DEFAULTS["max_replies_per_comment"])
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--force", action="store_true",
                         help="Re-crawl URLs even if already captured in an existing output file.")
    parser.add_argument("--batch-id")
    return parser


def crawl_article(url, *, max_comments_per_article, with_replies, max_replies_per_comment,
                   batch_id_value, handle_salt):
    """Returns (records, stop_reason). stop_reason is one of:
    "no_outlet", "no_widget", "gated_out", "no_comments", "ok"."""
    _outlet_key, outlet, adapter = _resolve_outlet(url)
    if outlet is None:
        return [], "no_outlet"

    html = fetch_text(url)
    title, body, published_at, ids = adapter.extract_meta(html)
    if ids is None:
        return [], "no_widget"

    # Re-gate before crawling comments -- a safety net against a changed
    # page, not a second veto over the discovery-CSV review (a human
    # already reviewed and kept this URL by the time it reaches --url-file).
    verdict, rule = is_on_topic_news(title, body=body, source=outlet)
    if verdict == "reject":
        return [], "gated_out"

    comments = list(adapter.fetch_comments(outlet, ids, DEFAULTS["comment_page_size"], max_comments_per_article))
    if not comments:
        return [], "no_comments"

    def _record(comment, *, is_reply=False, parent_native_comment_id=None):
        record = to_record(
            article_url=url,
            publisher=outlet["publisher_name"],
            title=title,
            published_at=published_at,
            comment=comment,
            batch_id_value=batch_id_value,
            handle_salt=handle_salt,
            topic_rule=rule,
            matched_groups_=matched_groups(comment.get("content", "")),
            discovery_query="",
            source_class=outlet["source_class"],
            is_reply=is_reply,
            parent_native_comment_id=parent_native_comment_id,
            discovery_mode=outlet.get("discovery_mode", "search"),
        )
        # Overwrite news_common.to_record()'s default id/sources_version
        # with this topic's own provenance markers (see module docstring).
        record["id"] = comment_record_id(url, comment["native_comment_id"])
        record["sources_version"] = SOURCES_VERSION
        return record

    records = [_record(comment) for comment in comments]

    if with_replies:
        for comment in comments:
            if comment.get("reply_count", 0) <= 0:
                continue
            replies = adapter.fetch_replies(
                outlet, ids, comment["native_comment_id"],
                comment["reply_count"], DEFAULTS["comment_page_size"], max_replies_per_comment,
            )
            records.extend(
                _record(reply, is_reply=True, parent_native_comment_id=comment["native_comment_id"])
                for reply in replies
            )

    return records, "ok"


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    handle_salt = require_env_salt("NEWS_HANDLE_SALT")
    urls = read_url_file(args.url_file)
    if not urls:
        print("[STOP] no URLs to crawl.")
        return

    os.makedirs(args.output_dir, exist_ok=True)
    batch = args.batch_id or batch_id("news_flood", now_hcm())
    out_path = os.path.join(args.output_dir, f"{batch}.jsonl")

    seen_urls = set() if args.force else load_seen_urls(args.output_dir)
    stats = {"attempted": 0, "skipped_seen": 0, "no_outlet": 0, "no_widget": 0,
              "no_comments": 0, "gated_out": 0, "ok": 0, "errored": 0}

    with JsonlWriter(out_path) as writer:
        for index, url in enumerate(urls):
            if index > 0:
                time.sleep(random.uniform(DEFAULTS["delay_min"], DEFAULTS["delay_max"]))

            print(f"\n=== [{index + 1}/{len(urls)}] {url} ===")
            if url in seen_urls:
                print("  already captured, skipping")
                stats["skipped_seen"] += 1
                continue

            stats["attempted"] += 1
            try:
                records, stop_reason = crawl_article(
                    url,
                    max_comments_per_article=args.max_comments_per_article,
                    with_replies=args.with_replies,
                    max_replies_per_comment=args.max_replies_per_comment,
                    batch_id_value=batch,
                    handle_salt=handle_salt,
                )
            except (NewsAPIError, urllib.error.URLError, OSError) as exc:
                print(f"  [ERROR] {exc!r}")
                stats["errored"] += 1
                continue

            stats[stop_reason] += 1
            if not records:
                print(f"  no comments written (stop_reason={stop_reason})")
                continue

            written = sum(1 for r in records if writer.write(r))
            print(f"  wrote {written} comments (of {len(records)} fetched)")

    print("\n=== Run summary ===")
    print(f"attempted / skipped_seen / no_outlet / no_widget / no_comments / gated_out / ok / errored: "
          f"{stats['attempted']} / {stats['skipped_seen']} / {stats['no_outlet']} / {stats['no_widget']} / "
          f"{stats['no_comments']} / {stats['gated_out']} / {stats['ok']} / {stats['errored']}")
    print(f"comments written: {writer.count}")
    print(f"Output: {out_path}")


if __name__ == "__main__":
    main()
