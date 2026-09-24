"""Isolated headful smoke cho Facebook baseline worker."""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from crawlers.baseline_qc import check as qc_check
from crawlers.baseline_store import BaselineStore
from crawlers.facebook import crawl_facebook as fb
from crawlers.facebook import facebook_session as fbs
from crawlers.facebook import facebook_worker as worker

PRODUCTION_DIR = os.path.abspath("data/outputs/baseline_gate_v2")
QUERY = "xe buýt miễn phí TP.HCM"


def _file_hash(path):
    if not os.path.exists(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _production_snapshot():
    db_path = os.path.join(PRODUCTION_DIR, "crawl.db")
    snapshot = {
        "facebook_comments_csv": _file_hash(os.path.join(PRODUCTION_DIR, "facebook_comments.csv")),
        "facebook_audit_csv": _file_hash(os.path.join(PRODUCTION_DIR, "facebook_post_audit.csv")),
    }
    if not os.path.exists(db_path):
        snapshot["db"] = None
        return snapshot
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        queries = {
            "contexts": "SELECT * FROM contexts WHERE platform='facebook' ORDER BY context_id",
            "comments": "SELECT * FROM comments WHERE platform='facebook' ORDER BY comment_id",
            "queue": "SELECT * FROM discovery_queue WHERE platform='facebook' ORDER BY id",
            "events": "SELECT * FROM events WHERE platform='facebook' ORDER BY id",
            "kv": "SELECT * FROM kv WHERE key LIKE 'facebook%' ORDER BY key",
        }
        snapshot["db"] = {
            key: [dict(row) for row in conn.execute(sql).fetchall()]
            for key, sql in queries.items()
        }
    finally:
        conn.close()
    return snapshot


def _youtube_pids():
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*run_youtube_only.py*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    return sorted(int(line) for line in result.stdout.splitlines() if line.strip().isdigit())


def _assert_smoke(store, selected_url, max_comments):
    contexts = store.conn.execute("SELECT * FROM contexts WHERE platform='facebook'").fetchall()
    comments = store.conn.execute("SELECT * FROM comments WHERE platform='facebook'").fetchall()
    queue = store.conn.execute("SELECT status FROM discovery_queue WHERE platform='facebook'").fetchall()
    events = store.conn.execute(
        "SELECT kind FROM events WHERE platform='facebook' AND kind IN ('contamination','fb_capture_error')"
    ).fetchall()

    assert len(contexts) == 1, f"expected 1 context, got {len(contexts)}"
    context = contexts[0]
    assert context["context_id"] == fb.canonical_post_identity(selected_url)
    assert fb.url_matches_target(context["source_url"], selected_url)
    assert context["verdict"] == "accept" and context["reason"] == "du_3_nhom"
    assert context["metadata_resolved"] == 1 and context["state"] == "comments_done"
    assert 1 <= len(comments) <= max_comments, f"expected 1..{max_comments} comments, got {len(comments)}"
    assert all(row["comment_text"].strip() for row in comments)
    assert all(row["posted_at_raw"] for row in comments)
    assert all(fb.parse_comment_tooltip(row["posted_at_raw"]) == row["posted_at"] for row in comments)
    assert all(row["posted_at"].endswith("+07:00") for row in comments)
    assert all(row["context_id"] == context["context_id"] for row in comments)
    assert all(fb.url_matches_target(row["source_url"], selected_url) for row in comments)
    assert all(row["comment_type"] in ("top_level", "reply") for row in comments)
    assert all(
        row["comment_type"] != "reply" or row["parent_comment_id"] or row["parent_unresolved"]
        for row in comments
    )
    assert queue and all(row["status"] == "done" for row in queue)
    assert not events

    qc = qc_check(store)
    assert qc["ok"], qc["issues"]
    exports = store.export_csv()
    assert exports["facebook_comments"][1] == len(comments)
    assert exports["facebook_audit"][1] == 1
    return qc, exports


def run_smoke(base_dir, max_comments=10, timeout_seconds=900, max_candidates=5, url=None):
    base_dir = os.path.abspath(base_dir)
    if os.path.commonpath((base_dir, PRODUCTION_DIR)) == PRODUCTION_DIR:
        raise ValueError("Smoke directory không được nằm trong production baseline directory")
    if os.path.exists(base_dir) and os.listdir(base_dir):
        raise ValueError("Smoke directory phải mới hoặc rỗng")

    production_before = _production_snapshot()
    youtube_before = _youtube_pids()

    store = BaselineStore(base_dir=base_dir)
    browser = None
    selected_url = None
    result = {"ok": False, "base_dir": base_dir, "query": QUERY, "candidates": []}
    deadline = time.monotonic() + timeout_seconds
    try:
        run = store.get_or_create_run(hours=1, target_minimum=1)
        browser = fbs.FacebookSession(headless=True).start()
        worker._SESSIONS.browser = browser
        fb.use_browser_session(browser)

        state, detail = browser.ensure_login()
        if state != fbs.AUTH_OK:
            raise RuntimeError(
                f"Facebook auth required ({state}); chạy "
                "python -m crawlers.facebook.facebook_session --interactive rồi retry smoke. "
                f"screenshot={detail.get('challenge_shot')}"
            )

        if url:
            selected_url = fb.canonical_post_url(url)
        else:
            original_scroll_rounds = fb.SCROLL_ROUNDS
            fb.SCROLL_ROUNDS = 3
            try:
                candidates = fb.search_posts(QUERY, max_candidates)
            finally:
                fb.SCROLL_ROUNDS = original_scroll_rounds
            for candidate_url in candidates:
                try:
                    metadata = fb.fetch_post_metadata(candidate_url)
                    verdict, reason = fb.gate_metadata(metadata)
                    result["candidates"].append({"url": candidate_url, "verdict": verdict, "reason": reason})
                except Exception as exc:
                    result["candidates"].append({"url": candidate_url, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
                    continue
                if verdict == "accept":
                    selected_url = candidate_url
                    break
            if not selected_url:
                raise RuntimeError(f"Không tìm thấy post accept trong {len(candidates)} candidates: {result['candidates']}")

        store.kv_set("facebook_seeded", True)
        store.enqueue(
            "facebook",
            "discover",
            {"kind": "direct_post", "label": "smoke", "source_class": "policy_search", "url": selected_url},
            priority=1,
        )
        while True:
            status = worker.run_once(store, run, lambda: time.monotonic() >= deadline, "facebook_smoke", max_comments)
            if status["status"] == "queue_empty":
                break
            if status["status"] == "auth_required":
                if time.monotonic() >= deadline:
                    raise TimeoutError("Facebook auth timeout giữa smoke")
                print("FACEBOOK_AUTH_REQUIRED: xử lý trong cửa sổ hiện tại", flush=True)
                time.sleep(2)
                continue
            raise RuntimeError(f"Smoke worker failed: {status}")

        qc, exports = _assert_smoke(store, selected_url, max_comments)
        production_after = _production_snapshot()
        youtube_after = _youtube_pids()
        assert production_after == production_before, "Facebook production state changed during smoke"
        assert youtube_after == youtube_before, "YouTube standalone process changed during smoke"
        result.update({
            "ok": True,
            "selected_url": selected_url,
            "comments": store.comment_count("facebook"),
            "qc": qc,
            "exports": {key: {"path": value[0], "rows": value[1]} for key, value in exports.items()},
            "youtube_pids": youtube_after,
        })
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)}"
        raise
    finally:
        try:
            worker.close_session()
        finally:
            store.close()
        os.makedirs(base_dir, exist_ok=True)
        result["selected_url"] = selected_url
        with open(os.path.join(base_dir, "smoke_result.json"), "w", encoding="utf-8") as file:
            json.dump(result, file, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-dir",
        default=os.path.join(
            "data", "outputs", "facebook_smoke", datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y%m%d_%H%M%S")
        ),
    )
    parser.add_argument("--url")
    parser.add_argument("--max-comments", type=int, default=10)
    parser.add_argument("--max-candidates", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    result = run_smoke(args.base_dir, args.max_comments, args.timeout_seconds, args.max_candidates, args.url)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
