"""Backfill: re-gate video YouTube cũ với description ĐẦY ĐỦ (không bị search.list cắt).

Đọc context_id/title/published_at từ legacy DB (đã search xong trước khi fix bug cắt
description), gọi videos.list lấy description đầy đủ, re-gate, ghi vào production
BaselineStore. Video đổi verdict reject->accept được crawl comment luôn.

Resumable qua kv "youtube_backfill_done_ids" — chạy lại chỉ xử lý phần còn thiếu.
"""
import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from crawlers.baseline_store import BaselineStore
from crawlers.topics import gate_post, matched_groups, co_thanh_pho_khac
from crawlers.youtube import crawl_youtube as yt
from crawlers.youtube.youtube_worker import _crawl_video
from datetime import datetime
from zoneinfo import ZoneInfo

BATCH_ID = "youtube_backfill"
RUN_ID = "youtube_backfill"


def _legacy_videos(legacy_db_path):
    conn = sqlite3.connect(f"file:{legacy_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT context_id, post_title, post_published_at_raw, post_published_at "
            "FROM contexts WHERE platform='youtube' ORDER BY context_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def run_backfill(legacy_db_path, batch_size=50, limit=None, base_dir="data/outputs/baseline_gate_v2"):
    store = BaselineStore(base_dir=base_dir)
    videos = _legacy_videos(legacy_db_path)
    if limit:
        videos = videos[:limit]
    done_ids = set(store.kv_get("youtube_backfill_done_ids", []))
    pending = [v for v in videos if v["context_id"] not in done_ids]
    print(f"total={len(videos)} da_xong={len(videos) - len(pending)} con_lai={len(pending)}")

    accepted_new = 0
    comments_added = 0
    processed = 0
    for offset in range(0, len(pending), batch_size):
        chunk = pending[offset:offset + batch_size]
        ids = [v["context_id"] for v in chunk]
        try:
            descriptions = yt.fetch_video_descriptions(ids)
        except yt.YouTubeAPIError as e:
            if e.reason == "quotaExceeded":
                print(f"QUOTA_EXCEEDED khi lay description. Da xu ly {processed}/{len(pending)}. Chay lai script se resume.")
                store.close()
                return
            raise

        for video in chunk:
            vid = video["context_id"]
            title = video["post_title"] or ""
            desc = descriptions.get(vid, "")
            verdict, reason = gate_post(title, desc)
            store.upsert_context({
                "platform": "youtube", "context_id": vid,
                "source_url": f"https://www.youtube.com/watch?v={vid}",
                "post_title": title, "post_context": f"{title}\n{desc}".strip(),
                "post_published_at_raw": video.get("post_published_at_raw", ""),
                "post_published_at": video.get("post_published_at", ""),
                "verdict": verdict, "reason": reason, "metadata_resolved": 1,
                "matched_groups": ",".join(matched_groups(title + " " + desc)),
                "co_thanh_pho_khac": int(co_thanh_pho_khac(title, desc)),
                "discovery_source": "backfill", "discovery_query": "backfill_full_description",
                "state": "accepted" if verdict == "accept" else "rejected",
                "crawled_at": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat(),
            })
            if verdict != "accept":
                done_ids.add(vid)
                continue
            accepted_new += 1
            crawl_video = {
                "video_id": vid, "title": title, "description": desc,
                "published_at": video.get("post_published_at", ""),
            }
            while True:
                try:
                    added = _crawl_video(store, crawl_video, RUN_ID, BATCH_ID)
                    comments_added += added
                    store.set_context_state("youtube", vid, "comments_done")
                    done_ids.add(vid)
                    break
                except yt.YouTubeAPIError as e:
                    if e.reason == "quotaExceeded":
                        print(f"QUOTA_EXCEEDED khi crawl comment. Da xu ly {processed}/{len(pending)}. Chay lai script se resume.")
                        store.kv_set("youtube_backfill_done_ids", sorted(done_ids))
                        store.export_csv()
                        store.close()
                        return
                    if e.reason == "rateLimitExceeded":
                        print("rateLimitExceeded, cho 900s...")
                        time.sleep(900)
                        continue
                    terminal = {"commentsDisabled": "comments_disabled", "videoNotFound": "video_not_found",
                                "commentNotFound": "comment_not_found"}
                    if e.reason in terminal:
                        store.set_context_state("youtube", vid, terminal[e.reason])
                        done_ids.add(vid)
                        break
                    print(f"loi comment video={vid} reason={e.reason}, bo qua")
                    done_ids.add(vid)
                    break
                except OSError as exc:
                    print(f"loi mang video={vid}: {exc!r}, thu lai sau 30s")
                    time.sleep(30)
            processed += 1

        store.kv_set("youtube_backfill_done_ids", sorted(done_ids))
        print(f"batch {offset // batch_size + 1}: xu ly {processed}/{len(pending)}, "
              f"accept_moi={accepted_new}, comment_them={comments_added}")

    store.export_csv()
    print(f"XONG. accept_moi={accepted_new}, comment_them={comments_added}")
    store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-db", default="data/outputs/baseline_gate_v2_legacy_20260731/crawl.db")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    run_backfill(args.legacy_db, args.batch_size, args.limit)


if __name__ == "__main__":
    main()
