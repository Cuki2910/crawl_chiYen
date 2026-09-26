"""One-off: re-run the fixed topic gate over an already-crawled dataset.

Updates crawl.db (contexts + comments) in place, then regenerates the
per-platform audit/comments CSVs and progress.json counters from the
updated DB so everything stays consistent with topics.py's current regex.
"""

import csv
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawlers.topics import co_thanh_pho_khac, gate_post, matched_groups

OUT_DIR = Path("data/outputs/baseline_gate_v2")

CONTEXT_AUDIT_FIELDS = [
    "platform", "context_id", "source_url", "post_title", "post_context",
    "post_published_at_raw", "post_published_at", "verdict", "reason",
    "metadata_resolved", "matched_groups", "co_thanh_pho_khac",
    "discovery_source", "discovery_query", "expected_comment_count",
    "captured_comment_count", "missing_comment_count",
    "comment_capture_status", "comment_retry_count", "crawled_at",
]
COMMENT_FIELDS = [
    "id", "platform", "source_url", "context_id", "post_context", "post_title",
    "post_published_at_raw", "post_published_at", "comment_text", "comment_type",
    "parent_comment_id", "thread_id", "reply_depth", "posted_at_raw", "posted_at",
    "likes_count", "author_key", "matched_groups", "co_thanh_pho_khac",
    "parent_unresolved", "crawl_batch_id", "run_id", "crawled_at",
]


def regate_contexts(con):
    cur = con.execute("SELECT platform, context_id, post_title, post_context, state FROM contexts")
    rows = cur.fetchall()
    changed = 0
    for platform, context_id, title, body, current_state in rows:
        title, body = title or "", body or ""
        verdict, reason = gate_post(title, body)
        groups = ",".join(matched_groups(f"{title} {body}"))
        other_city = int(co_thanh_pho_khac(title, body))
        if verdict != "accept":
            state = "rejected"
        elif current_state == "rejected":
            state = "accepted"
        else:
            state = current_state or "accepted"
        con.execute(
            "UPDATE contexts SET verdict=?, reason=?, matched_groups=?, co_thanh_pho_khac=?, state=? "
            "WHERE platform=? AND context_id=?",
            (verdict, reason, groups, other_city, state, platform, context_id),
        )
        changed += 1
    return changed


def purge_nonaccepted_comments(con):
    """Remove comments whose parent context is absent or no longer accepted."""
    cur = con.execute(
        "DELETE FROM comments "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM contexts x "
        "WHERE x.platform=comments.platform AND x.context_id=comments.context_id "
        "AND x.verdict='accept'"
        ")"
    )
    return cur.rowcount


def regate_comments(con):
    cur = con.execute("SELECT id, platform, comment_text, post_title, post_context FROM comments")
    rows = cur.fetchall()
    for comment_id, platform, text, title, body in rows:
        title, body = title or "", body or ""
        groups = ",".join(matched_groups(text or ""))
        other_city = int(co_thanh_pho_khac("" if platform == "facebook" else title, body))
        con.execute(
            "UPDATE comments SET matched_groups=?, co_thanh_pho_khac=? WHERE id=?",
            (groups, other_city, comment_id),
        )
    return len(rows)


def export_context_csv(con, platform, path):
    cur = con.execute(
        f"SELECT {','.join(CONTEXT_AUDIT_FIELDS)} FROM contexts WHERE platform=? ORDER BY context_id",
        (platform,),
    )
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(CONTEXT_AUDIT_FIELDS)
        w.writerows(cur.fetchall())


def export_comment_csv(con, platform, path):
    fields = ",".join(f"c.{field}" for field in COMMENT_FIELDS)
    cur = con.execute(
        f"SELECT {fields} FROM comments c "
        "JOIN contexts x ON x.platform=c.platform AND x.context_id=c.context_id "
        "WHERE c.platform=? AND x.verdict='accept' ORDER BY c.id",
        (platform,),
    )
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(COMMENT_FIELDS)
        w.writerows(cur.fetchall())


def update_progress(con):
    path = OUT_DIR / "progress.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for platform in ("facebook", "youtube"):
        accepted, rejected = con.execute(
            "SELECT SUM(verdict='accept'), SUM(verdict='reject') FROM contexts WHERE platform=?",
            (platform,),
        ).fetchone()
        data[f"{platform}_accepted"] = accepted or 0
        data[f"{platform}_rejected"] = rejected or 0
    total_unique_valid = con.execute(
        "SELECT COUNT(*) FROM comments c JOIN contexts x ON c.context_id = x.context_id "
        "AND c.platform = x.platform WHERE x.verdict='accept'"
    ).fetchone()[0]
    data["total_unique_valid"] = total_unique_valid
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    db_path = OUT_DIR / "crawl.db"
    con = sqlite3.connect(db_path)
    try:
        before = dict(con.execute("SELECT verdict, COUNT(*) FROM contexts GROUP BY verdict").fetchall())
        n_ctx = regate_contexts(con)
        n_deleted = purge_nonaccepted_comments(con)
        n_com = regate_comments(con)
        con.commit()
        after = dict(con.execute("SELECT verdict, COUNT(*) FROM contexts GROUP BY verdict").fetchall())

        export_context_csv(con, "facebook", OUT_DIR / "facebook_post_audit.csv")
        export_context_csv(con, "youtube", OUT_DIR / "youtube_post_audit.csv")
        export_comment_csv(con, "facebook", OUT_DIR / "facebook_comments.csv")
        export_comment_csv(con, "youtube", OUT_DIR / "youtube_comments.csv")
        update_progress(con)

        print(f"contexts regated: {n_ctx}, comments purged: {n_deleted}, comments regated: {n_com}")
        print(f"verdicts before: {before}")
        print(f"verdicts after:  {after}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
