"""Threads reply crawler for the Hanoi-flood-impact-on-commuting topic --
takes a human-reviewed URL list from discover_threads.py's candidate CSV,
fetches each post's replies via DOM scroll+read (same discover-then-crawl
split as crawl_tiktok.py, kept separate so a block during crawl never
touches the discovery output).

NOTE: reply-card extraction below is a best-effort placeholder (see
discover_threads.py's module docstring) -- not confirmed against live
Threads markup. Verify/adjust extract_replies_from_page() against the real
post-detail page before trusting this for an unattended run.
"""

import argparse
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from crawlers.common.records import (  # noqa: E402
    JsonlWriter,
    batch_id,
    now_hcm,
    record_id,
    require_env_salt,
    salted_hash,
)
from crawlers.topics import is_on_topic_threads, is_on_topic_threads_electric_bus, matched_groups  # noqa: E402
from crawlers.threads.threads_session import (  # noqa: E402
    BlockedError,
    SessionExpiredError,
    check_block_markers,
    launch_context,
)

OUTPUT_DIR = "data/outputs/threads"
SOURCES_VERSION = "threads_flood_sources_v1_2026-09-27"

DEFAULTS = {
    # Raised from 60/5 -- confirmed live those stopped a scroll at 139 of
    # 275 actual top-level replies on a busy post (stale_rounds hit before
    # reaching the bottom, not because it actually ran out of content;
    # Threads' lazy-load can be slower than 5 consecutive scrolls'
    # worth of patience). 200/15 costs more time on quiet posts but
    # avoids silently truncating busy ones.
    "max_rounds": 200,
    "stale_rounds": 15,
    "delay_min": 15,
    "delay_max": 45,
    "max_posts": 50,
    # Effectively unbounded -- Threads reply chains can run many levels
    # deep (confirmed live) and there's no reliable way to know the true
    # depth in advance, so capping low just produces misattributed
    # records once the budget runs out (see _records_from_pool's
    # docstring). Pass --max-reply-depth explicitly to cap it for a
    # faster/cheaper test run.
    "max_reply_depth": 1000,
}

_POST_ID_PATTERN = re.compile(r"/post/([A-Za-z0-9_-]+)")
_HASHTAG_PATTERN = re.compile(r"#(\w+)", re.UNICODE)


def extract_hashtags(text):
    return [f"#{tag}" for tag in _HASHTAG_PATTERN.findall(text or "")]


def canonical_post_url(url):
    """Strip tracking query params, keep the real post path. Preserves the
    input's own domain rather than hardcoding one -- confirmed live, real
    Threads post URLs use threads.com (e.g.
    https://www.threads.com/@handle/post/<id>), not threads.net (which
    still works for login/search navigation, but is NOT the domain post
    permalinks resolve to). Rewriting to a hardcoded domain here was a bug:
    it silently turned every correct threads.com URL into a threads.net
    one that likely wouldn't resolve the same content."""
    parsed = urllib.parse.urlparse(url)
    path = re.sub(r"/+$", "", parsed.path) or "/"
    netloc = parsed.netloc or "www.threads.com"
    return urllib.parse.urlunparse(("https", netloc, path, "", "", ""))


def post_id_from_url(url):
    match = _POST_ID_PATTERN.search(url)
    return match.group(1) if match else ""


# --------------------------------------------------------------------------
# Pure functions -- reply parsing, dedup, record mapping (offline-testable)
# --------------------------------------------------------------------------


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_reply_card(card):
    """One raw extracted reply-card dict -> a normalized reply, or None if
    required fields are missing. ``reply_count`` is best-effort (0 when not
    detected) -- it only gates whether crawl_post() bothers descending into
    that reply's own sub-thread, never blocks the reply itself from being
    recorded."""
    reply_id = card.get("reply_id") or ""
    if not reply_id:
        return None
    return {
        "reply_id": reply_id,
        "reply_text": card.get("reply_text", "") or "",
        "posted_at_epoch": _safe_int(card.get("posted_at_epoch")),
        "likes_count": _safe_int(card.get("likes_count")),
        "author_handle": card.get("author_handle", "") or "",
        "reply_count": _safe_int(card.get("reply_count")),
    }


def dedup_pool(existing, new_replies):
    merged = dict(existing)
    added = 0
    for reply in new_replies:
        if reply["reply_id"] not in merged:
            merged[reply["reply_id"]] = reply
            added += 1
    return merged, added


def reply_record_id(post_id, reply_id):
    return record_id("threads_flood_c", post_id, reply_id)


def to_record(
    *,
    post_url,
    post_id,
    post_text,
    reply,
    batch_id_value,
    handle_salt,
    discovery_query="",
    discovery_url="",
    source_kind="",
    source_class="",
    source_label="",
    hashtags=(),
    topic_rule="",
    matched_groups_=(),
    posted_at="",
    parent_id=None,
    depth=1,
):
    return {
        "id": reply_record_id(post_id, reply["reply_id"]),
        "platform": "threads",
        "source_url": post_url,
        "post_context": post_text,
        "post_posted_at": posted_at,
        "comment_text": reply["reply_text"],
        "posted_at_raw": datetime.fromtimestamp(reply["posted_at_epoch"]).isoformat()
        if reply["posted_at_epoch"] else "",
        "likes_count": reply["likes_count"],
        "crawled_at": now_hcm().isoformat(),
        "crawl_batch_id": batch_id_value,
        "author_hash": salted_hash(reply["author_handle"], handle_salt),
        "post_id": post_id,
        # is_reply is always True here -- every record from this crawler is
        # a reply to something (the root post, or another reply). depth=1
        # means "direct reply to the root post"; depth=2+ means a reply to
        # another reply, nested that many levels deep. parent_id is the
        # record id of whichever reply (or the root post, via
        # reply_record_id with the post itself) this one replied to.
        "is_reply": True,
        "parent_id": parent_id,
        "depth": depth,
        "reply_count": reply.get("reply_count", 0),
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
# Browser plumbing -- DOM extraction (placeholder selectors, see docstring)
# --------------------------------------------------------------------------

_EXTRACT_POST_META_JS = """
() => {
    const mainPost = document.querySelector('div[data-pressable-container]');
    const text = mainPost ? mainPost.innerText.slice(0, 2000) : (document.body.innerText || '').slice(0, 2000);
    const timeEl = document.querySelector('time');
    return { post_text: text, posted_at_iso: timeEl ? timeEl.getAttribute('datetime') : null };
}
"""

_EXTRACT_REPLY_CARDS_JS = """
() => {
    const containers = [...document.querySelectorAll('div[data-pressable-container]')].slice(1);
    const results = [];
    for (const c of containers) {
        const link = c.querySelector('a[href*="/post/"]');
        const match = link ? (link.getAttribute('href') || '').match(/\\/post\\/([A-Za-z0-9_-]+)/) : null;
        if (!match) continue;
        const authorLink = c.querySelector('a[href^="/@"]');
        const timeEl = c.querySelector('time');
        // Confirmed live: the reply-count signal is the "Reply" action
        // icon (an <svg><title>Reply</title></svg>, same icon used under
        // the main post), with a sibling element holding the bare number
        // (no "replies" word in the text at all -- the old text-pattern
        // guess never matched). Scoped to this icon's immediate parent
        // div so it doesn't pick up the like/repost/share counts sitting
        // next to it in the same action row.
        let replyCount = 0;
        for (const svg of c.querySelectorAll('svg')) {
            const titleEl = svg.querySelector('title');
            if (!titleEl || titleEl.textContent.trim().toLowerCase() !== 'reply') continue;
            const container = svg.closest('div');
            if (!container) break;
            const numEl = [...container.querySelectorAll('span, div')].find(
                el => /^\\d+$/.test((el.textContent || '').trim())
            );
            if (numEl) replyCount = numEl.textContent.trim();
            break;
        }
        results.push({
            reply_id: match[1],
            reply_text: (c.innerText || '').slice(0, 2000),
            author_handle: authorLink ? (authorLink.getAttribute('href') || '').replace(/^\\/@/, '') : '',
            posted_at_iso: timeEl ? timeEl.getAttribute('datetime') : null,
            reply_count: replyCount,
        });
    }
    return results;
}
"""


def _iso_to_epoch(iso_string):
    if not iso_string:
        return 0
    try:
        return int(datetime.fromisoformat(iso_string.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def extract_post_meta(page):
    meta = page.evaluate(_EXTRACT_POST_META_JS)
    return {
        "post_text": meta.get("post_text", ""),
        "posted_at_epoch": _iso_to_epoch(meta.get("posted_at_iso")),
    }


def extract_replies_from_page(page):
    raw_cards = page.evaluate(_EXTRACT_REPLY_CARDS_JS)
    for raw in raw_cards:
        raw["posted_at_epoch"] = _iso_to_epoch(raw.get("posted_at_iso"))
    return raw_cards


def build_pool(page, max_rounds, stale_rounds):
    pool = {}
    stale = 0
    for _round_index in range(max_rounds):
        page.mouse.wheel(0, 1200)
        page.wait_for_timeout(1500)
        check_block_markers(page)
        raw_cards = extract_replies_from_page(page)
        normalized = [r for card in raw_cards if (r := normalize_reply_card(card)) is not None]
        pool, added = dedup_pool(pool, normalized)
        if added == 0:
            stale += 1
            if stale >= stale_rounds:
                return pool, "stale_rounds"
        else:
            stale = 0
    return pool, "max_rounds"


def _reply_url(reply):
    """Every Threads reply is itself a post at /@<handle>/post/<id>
    (confirmed live via real discovery output -- domain is threads.com,
    not threads.net)."""
    handle = reply.get("author_handle") or ""
    if handle:
        return f"https://www.threads.com/@{handle}/post/{reply['reply_id']}"
    # No handle captured for this reply -- fall back to the bare
    # /post/<id> form. Unconfirmed whether Threads resolves this without
    # the @handle prefix; if nested descent silently yields nothing for
    # replies missing a handle, this fallback is why.
    return f"https://www.threads.com/post/{reply['reply_id']}"


def _fetch_nested_replies(
    page, post_id, parent_reply, parent_record_id, *,
    depth, max_depth, max_rounds, stale_rounds, batch_id_value, handle_salt,
    source, canonical_url, post_text, hashtags, topic_rule, posted_at_iso, visited,
):
    """Navigate into ``parent_reply``'s own post page and fetch its
    replies. Only called for reply_count >= 2 -- those aren't rendered on
    the current page at all until clicked (unlike reply_count == 1, whose
    single child renders inline and is handled by _records_from_pool
    without navigation).

    A failed navigation (dead link, block mid-descent) only drops that one
    branch -- it doesn't raise and kill the whole crawl, except for
    BlockedError/SessionExpiredError, which must still stop the run (same
    convention as the rest of this module)."""
    try:
        page.goto(_reply_url(parent_reply), wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(2000)
        check_block_markers(page)
    except (BlockedError, SessionExpiredError):
        raise
    except Exception:
        return []

    pool_dict, _stop_reason = build_pool(page, max_rounds, stale_rounds)
    nested_pool = list(pool_dict.values())

    return _records_from_pool(
        nested_pool, page=page, post_id=post_id, canonical_url=canonical_url,
        post_text=post_text, hashtags=hashtags, topic_rule=topic_rule,
        posted_at_iso=posted_at_iso, batch_id_value=batch_id_value, handle_salt=handle_salt,
        source=source, start_parent_id=parent_record_id, start_depth=depth,
        max_depth=max_depth, max_rounds=max_rounds, stale_rounds=stale_rounds, visited=visited,
    )


def _records_from_pool(
    pool, *, page, post_id, canonical_url, post_text, hashtags, topic_rule, posted_at_iso,
    batch_id_value, handle_salt, source, start_parent_id, start_depth, max_depth,
    max_rounds, stale_rounds, visited,
):
    """Walk a flat, document-ordered reply pool (one page's worth, from
    build_pool) and emit records with correct parent_id/depth.

    Confirmed live: a reply with reply_count == 1 has its single child
    rendered INLINE as the very next card in the same pool, in document
    order -- no navigation needed, just attach it to the reply that
    precedes it and continue the chain (which can itself have
    reply_count == 1 and so on, arbitrarily deep). reply_count >= 2 is
    NOT rendered inline at all -- only resolvable via _fetch_nested_replies
    navigating to that reply's own page. Without this distinction, the
    inline chain members get flattened into the pool looking like ordinary
    top-level siblings with no way to tell them apart -- there is no
    reliable DOM/class signal for "this card is an inline child" otherwise
    (confirmed live: the only class differences between chain members are
    arbitrary per-card atomic-CSS hashes, not a stable nesting marker).

    ``visited`` is a set of reply_ids already processed anywhere in this
    post's crawl, shared across every recursive call (mutated in place).
    Confirmed live this is necessary, not just defensive: navigating into
    a reply's own page can yield that SAME reply back as one of its own
    "replies" (the extraction's assumption that the page's first
    data-pressable-container is always the main post and safe to skip
    breaks on at least some page layouts -- a pinned post is the one
    confirmed case so far), which without this guard becomes an infinite
    navigate-to-self loop once max_reply_depth stopped being a low enough
    cap to mask it."""
    records = []
    expect_inline_child = False
    chain_parent_id, chain_depth = start_parent_id, start_depth
    for reply in pool:
        reply_id = reply.get("reply_id")
        if reply_id in visited:
            expect_inline_child = False
            continue
        visited.add(reply_id)

        if expect_inline_child:
            parent_id, depth = chain_parent_id, chain_depth
        else:
            parent_id, depth = start_parent_id, start_depth

        record = to_record(
            post_url=canonical_url,
            post_id=post_id,
            post_text=post_text,
            reply=reply,
            batch_id_value=batch_id_value,
            handle_salt=handle_salt,
            source_kind=source.get("kind", ""),
            source_class=source.get("source_class", ""),
            source_label=source.get("label", ""),
            discovery_query=source.get("query", ""),
            discovery_url=source.get("url", ""),
            hashtags=hashtags,
            topic_rule=topic_rule,
            matched_groups_=matched_groups(reply["reply_text"]),
            posted_at=posted_at_iso,
            parent_id=parent_id,
            depth=depth,
        )
        records.append(record)

        count = reply.get("reply_count", 0)
        if count == 1 and depth < max_depth:
            expect_inline_child = True
            chain_parent_id, chain_depth = record["id"], depth + 1
        else:
            expect_inline_child = False
            if count > 1 and depth < max_depth:
                records.extend(_fetch_nested_replies(
                    page, post_id, reply, record["id"],
                    depth=depth + 1, max_depth=max_depth,
                    max_rounds=max_rounds, stale_rounds=stale_rounds,
                    batch_id_value=batch_id_value, handle_salt=handle_salt,
                    source=source, canonical_url=canonical_url, post_text=post_text,
                    hashtags=hashtags, topic_rule=topic_rule, posted_at_iso=posted_at_iso,
                    visited=visited,
                ))
    return records


def crawl_post(page, post_url, *, max_rounds, stale_rounds, batch_id_value, handle_salt,
                source=None, max_reply_depth=DEFAULTS["max_reply_depth"], gate=is_on_topic_threads):
    """max_reply_depth=1 only collects direct replies to the post (old
    behavior); 2+ also descends into each reply's own sub-thread that many
    levels deep -- see DEFAULTS["max_reply_depth"] and build_arg_parser()'s
    --max-reply-depth help for why the default is effectively unbounded."""
    source = source or {}
    canonical_url = canonical_post_url(post_url)
    post_id = post_id_from_url(canonical_url)

    page.goto(canonical_url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(3000)
    check_block_markers(page)

    meta = extract_post_meta(page)
    hashtags = extract_hashtags(meta["post_text"])

    # Gate already ran at discovery time -- this is a defensive re-check
    # only, not a second gate (same convention as crawl_tiktok.py).
    topic_verdict, topic_rule = gate(meta["post_text"], hashtags, source=source)
    if topic_verdict != "accept":
        return [], f"topic_reject:{topic_rule}"

    pool_dict, stop_reason = build_pool(page, max_rounds, stale_rounds)
    pool = list(pool_dict.values())
    if not pool:
        return [], stop_reason

    posted_at_iso = (
        datetime.fromtimestamp(meta["posted_at_epoch"]).isoformat() if meta["posted_at_epoch"] else ""
    )

    records = _records_from_pool(
        pool, page=page, post_id=post_id, canonical_url=canonical_url,
        post_text=meta["post_text"], hashtags=hashtags, topic_rule=topic_rule,
        posted_at_iso=posted_at_iso, batch_id_value=batch_id_value, handle_salt=handle_salt,
        source=source, start_parent_id=post_id, start_depth=1, max_depth=max_reply_depth,
        max_rounds=max_rounds, stale_rounds=stale_rounds, visited={post_id},
    )
    return records, stop_reason


# --------------------------------------------------------------------------
# Discovery -- URL file (same BOM-sniffing convention as crawl_tiktok.py)
# --------------------------------------------------------------------------


def read_url_file(path):
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


CAMPAIGN_GATES = {
    "flood_transport": is_on_topic_threads,
    "electric_bus": is_on_topic_threads_electric_bus,
}
CAMPAIGN_BATCH_PREFIX = {
    "flood_transport": "threads_flood",
    "electric_bus": "threads_electric_bus",
}


def build_arg_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", choices=tuple(CAMPAIGN_GATES), default="flood_transport")
    parser.add_argument("--url-file", required=True,
                         help="Newline-delimited post URLs. Discovery lives in "
                              "discover_threads.py, not here.")
    parser.add_argument("--max-rounds", type=_positive_int, default=DEFAULTS["max_rounds"])
    parser.add_argument("--stale-rounds", type=_positive_int, default=DEFAULTS["stale_rounds"])
    parser.add_argument("--max-reply-depth", type=_positive_int, default=DEFAULTS["max_reply_depth"],
                         help="1 = only direct replies to the post. 2+ also descends into each "
                              "reply's own sub-thread that many levels deep. Defaults to "
                              "effectively unbounded (1000) -- reply_count==1 chains are "
                              "resolved from the already-fetched page with no extra cost, "
                              "only reply_count>=2 branches cost an extra navigation each, so "
                              "capping this low mainly just produces misattributed records "
                              "once the budget runs out rather than saving much work. Lower "
                              "it for a faster/cheaper test run.")
    parser.add_argument("--delay-min", type=_positive_int, default=DEFAULTS["delay_min"])
    parser.add_argument("--delay-max", type=_positive_int, default=DEFAULTS["delay_max"])
    parser.add_argument("--headful", dest="headless", action="store_false", default=None)
    parser.add_argument("--headless", dest="headless", action="store_true")
    parser.add_argument("--max-posts", type=_positive_int, default=DEFAULTS["max_posts"])
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--batch-id")
    parser.add_argument("--user-data-dir")
    return parser


def main(argv=None):
    import random

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    handle_salt = require_env_salt("THREADS_HANDLE_SALT")

    urls = read_url_file(args.url_file)[: args.max_posts]
    if not urls:
        print("[STOP] no URLs to crawl.")
        return

    gate = CAMPAIGN_GATES[args.campaign]
    headless = bool(args.headless)
    batch = args.batch_id or batch_id(CAMPAIGN_BATCH_PREFIX[args.campaign], now_hcm())
    out_path = os.path.join(args.output_dir, f"{batch}.jsonl")

    stats = {"attempted": 0, "succeeded": 0, "skipped_no_replies": 0, "skipped_off_topic": 0, "skipped_error": 0}
    stop_reason_summary = "completed"

    with launch_context(headless=headless, user_data_dir=args.user_data_dir) as (page, _context), \
         JsonlWriter(out_path) as writer:
        for index, url in enumerate(urls):
            if index > 0:
                delay = random.uniform(args.delay_min, args.delay_max)
                print(f"\nWait {delay:.1f}s before next post...")
                time.sleep(delay)

            print(f"\n=== [{index + 1}/{len(urls)}] {url} ===")
            stats["attempted"] += 1
            try:
                records, stop_reason = crawl_post(
                    page, url,
                    max_rounds=args.max_rounds,
                    stale_rounds=args.stale_rounds,
                    batch_id_value=batch,
                    handle_salt=handle_salt,
                    max_reply_depth=args.max_reply_depth,
                    gate=gate,
                )
            except (BlockedError, SessionExpiredError) as exc:
                print(f"[STOP] {type(exc).__name__}: {exc}")
                print(f"[STOP] Posts completed before stop: {stats['succeeded']}/{len(urls)}")
                print(f"[STOP] Resume hint: re-run with the same --url-file, skipping the first {index} URLs.")
                stop_reason_summary = type(exc).__name__
                break
            except Exception as exc:
                print(f"[SKIP] post={url} error={exc!r}")
                stats["skipped_error"] += 1
                continue

            if not records:
                if stop_reason.startswith("topic_reject:"):
                    print(f"  [TOPIC SKIP] {stop_reason}")
                    stats["skipped_off_topic"] += 1
                else:
                    print(f"  no replies (stop_reason={stop_reason})")
                    stats["skipped_no_replies"] += 1
                continue

            written = sum(1 for r in records if writer.write(r))
            stats["succeeded"] += 1
            print(f"  wrote {written} records (stop_reason={stop_reason})")

        print("\n=== Run summary ===")
        print(f"posts attempted / succeeded / skipped_no_replies / skipped_off_topic / skipped_error: "
              f"{stats['attempted']} / {stats['succeeded']} / {stats['skipped_no_replies']} / "
              f"{stats['skipped_off_topic']} / {stats['skipped_error']}")
        print(f"replies written: {writer.count}")
        print(f"stop reason: {stop_reason_summary}")
        print(f"Output: {out_path}")


if __name__ == "__main__":
    main()
