"""YouTube worker cho baseline_gate_v2.

Discovery = (keyword x cửa sổ năm). Gate title+description. Video accept lấy
top-level comment + toàn bộ reply. Checkpoint cursor/quota vào SQLite để resume.

Retention: dữ liệu comment API phải xóa/refresh trong 30 ngày kể từ crawled_at.
"""
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from crawlers.youtube import crawl_youtube as yt
from crawlers.hanoi_flood_transport.config import post_in_window, youtube_windows
from crawlers.topics import co_thanh_pho_khac, gate_post, matched_groups

# Cửa sổ năm cố định thay vì bisection thích nghi: dedup video ID xử lý trùng lặp,
# và search order=date trong mỗi năm đã đủ phủ. Đơn giản, ít quota lãng phí hơn.
# ponytail: cửa sổ năm cứng; nâng lên bisection nếu 1 năm bão hòa >nhiều trang.
YEAR_START = 2005  # YouTube public launch; không đặt giới hạn lịch sử nghiệp vụ.


def _windows():
    return list(youtube_windows())


def seed_queue(store):
    """Nạp (keyword x window) vào discovery_queue nếu chưa seed."""
    if store.kv_get("youtube_seeded"):
        return
    for keyword in yt.KEYWORDS:
        for after, before in _windows():
            store.enqueue("youtube", "search",
                          {"keyword": keyword, "after": after, "before": before},
                          priority=100)
    store.kv_set("youtube_seeded", True)


def _revisit_current_year(store):
    """Nạp lại cửa sổ năm hiện tại để bắt video mới đăng trong lúc job đang chạy.

    Seed ban đầu là snapshot lịch sử cố định; sau khi done, không còn task nào
    tự sinh ra để phát hiện video mới. dedup_key theo giờ (stamp) tránh spam
    quota search.list khi hàng đợi rỗng được kiểm tra lại mỗi vài phút.
    """
    now = datetime.now(timezone.utc)
    after, before = list(youtube_windows(now))[-1]
    stamp = now.strftime("%Y%m%d%H")
    for keyword in yt.KEYWORDS:
        store.enqueue("youtube", "search", {"keyword": keyword, "after": after, "before": before},
                      priority=50, dedup_key=f"youtube:revisit:{keyword}:{stamp}")


_MAX_REFRESH_RETRIES = 3  # deficit không giảm sau ngần này lần (comment API đếm nhưng không lấy được) -> ngừng re-queue.


def seed_deficit_refresh(store):
    """Rank accepted videos by API commentCount - stored_count, enqueue largest first."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    if store.kv_get("youtube_deficit_ranked_hour") == stamp:
        return 0
    rows = store.conn.execute(
        """SELECT x.context_id, COUNT(c.comment_id) stored_count
           FROM contexts x LEFT JOIN comments c
             ON c.platform=x.platform AND c.context_id=x.context_id
           WHERE x.platform='youtube' AND x.verdict='accept'
             AND x.state NOT IN ('comments_disabled','video_not_found')
             AND COALESCE(x.comment_retry_count,0) < ?
           GROUP BY x.context_id""",
        (_MAX_REFRESH_RETRIES,),
    ).fetchall()
    stored = {row["context_id"]: row["stored_count"] for row in rows}
    ids = list(stored)
    counts = {}
    for offset in range(0, len(ids), 50):
        counts.update(yt.fetch_video_comment_counts(ids[offset:offset + 50]))
        if offset + 50 < len(ids):
            time.sleep(1)
    queued = 0
    for video_id, api_count in counts.items():
        deficit = max(0, api_count - stored[video_id])
        if deficit <= 0:
            continue
        payload = {"video_id": video_id, "comment_count": api_count, "stored_count": stored[video_id], "deficit": deficit}
        if store.enqueue("youtube", "refresh_comments", payload, priority=-deficit,
                         dedup_key=f"youtube:refresh:{video_id}:{api_count}:{stamp}"):
            queued += 1
    store.kv_set("youtube_deficit_ranked_hour", stamp)
    store.log_event("youtube_deficit_ranked", "youtube", {"videos": len(ids), "queued": queued})
    return queued


def _video_from_context(store, video_id):
    row = store.conn.execute(
        "SELECT context_id,post_title,post_context,post_published_at FROM contexts WHERE platform='youtube' AND context_id=?",
        (video_id,),
    ).fetchone()
    if not row:
        return None
    title = row["post_title"] or ""
    context = row["post_context"] or ""
    prefix = f"{title}\n"
    return {
        "video_id": row["context_id"], "title": title,
        "description": context[len(prefix):] if context.startswith(prefix) else context,
        "published_at": row["post_published_at"] or "",
    }


def _record(video, snippet, comment_id, comment_type, parent_id, thread_id, depth, run_id, batch_id):
    title = video.get("title", "")
    desc = video.get("description", "")
    text = snippet.get("textDisplay", "")
    return {
        "platform": "youtube",
        "comment_id": comment_id,
        "context_id": video["video_id"],
        "id": f"youtube_c_{comment_id}",
        "source_url": f"https://www.youtube.com/watch?v={video['video_id']}",
        "post_context": f"{title}\n{desc}".strip(),
        "post_title": title,
        "post_published_at_raw": video.get("published_at", ""),
        "post_published_at": video.get("published_at", ""),
        "comment_text": text,
        "comment_type": comment_type,
        "parent_comment_id": parent_id,
        "thread_id": thread_id,
        "reply_depth": depth,
        "posted_at_raw": snippet.get("publishedAt", ""),
        "posted_at": snippet.get("publishedAt", ""),
        "likes_count": snippet.get("likeCount", 0),
        "author_key": snippet.get("authorChannelId", {}).get("value") if isinstance(snippet.get("authorChannelId"), dict) else None,
        "matched_groups": ",".join(matched_groups(text)),
        "co_thanh_pho_khac": int(co_thanh_pho_khac(title, desc)),
        "parent_unresolved": 0,
        "crawl_batch_id": batch_id,
        "run_id": run_id,
        "crawled_at": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat(),
    }


def _crawl_video(store, video, run_id, batch_id):
    """Top-level + toàn bộ replies. Trả về số comment mới thêm vào store."""
    added = 0
    for thread in yt.fetch_comments(video["video_id"]):
        top = thread["snippet"]["topLevelComment"]
        top_id = top["id"]
        batch = [_record(video, top["snippet"], top_id, "top_level", None, top_id, 0, run_id, batch_id)]
        if thread["snippet"].get("totalReplyCount", 0):
            for reply in yt.fetch_replies(top_id):
                batch.append(_record(video, reply["snippet"], reply["id"], "reply", top_id, top_id, 1, run_id, batch_id))
        added += store.add_comments(batch)
    return added


def _rate_limit_wait(store, task_id=None):
    failures = min(int(store.kv_get("youtube_rate_limit_failures", 0)) + 1, 3)
    delay = (900, 1800, 3600)[failures - 1]
    store.kv_set("youtube_rate_limit_failures", failures)
    if task_id is not None:
        store.defer_task(task_id, delay=delay)
    store.kv_set("youtube_status", "rate_limit_wait")
    store.log_event("rate_limit", "youtube", {"retry_seconds": delay})
    return {"status": "rate_limit_wait", "retry_seconds": delay}


def _drain_accepted_contexts(store, run, deadline_check, batch_id, processed_videos):
    rows = store.conn.execute(
        """SELECT context_id, post_title, post_context, post_published_at
           FROM contexts WHERE platform='youtube' AND state='accepted'
           ORDER BY crawled_at"""
    ).fetchall()
    for row in rows:
        if deadline_check():
            return {"status": "deadline"}
        title = row["post_title"] or ""
        context = row["post_context"] or ""
        prefix = f"{title}\n"
        video = {
            "video_id": row["context_id"],
            "title": title,
            "description": context[len(prefix):] if context.startswith(prefix) else context,
            "published_at": row["post_published_at"] or "",
        }
        try:
            _crawl_video(store, video, run["run_id"], batch_id)
        except yt.YouTubeAPIError as e:
            if e.reason == "quotaExceeded":
                store.kv_set("youtube_status", "quota_wait")
                store.log_event("quota", "youtube", {"endpoint": "comments"})
                return {"status": "quota_wait"}
            if e.reason == "rateLimitExceeded":
                return _rate_limit_wait(store)
            terminal_states = {
                "commentsDisabled": "comments_disabled",
                "videoNotFound": "video_not_found",
                "commentNotFound": "comment_not_found",
            }
            if e.reason in terminal_states:
                store.set_context_state("youtube", video["video_id"], terminal_states[e.reason])
                processed_videos.add(video["video_id"])
                continue
            store.log_event("yt_comment_error", "youtube", {"video": video["video_id"], "reason": e.reason})
            return {"status": "retry_wait", "retry_seconds": 60}
        except OSError as exc:
            store.log_event("yt_comment_error", "youtube", {"video": video["video_id"], "reason": repr(exc)[:200]})
            return {"status": "retry_wait", "retry_seconds": 60}
        store.set_context_state("youtube", video["video_id"], "comments_done")
        processed_videos.add(video["video_id"])
        store.kv_set("youtube_processed_videos", list(processed_videos))
        store.kv_set("youtube_rate_limit_failures", 0)
    return None


def run_once(store, run, deadline_check, batch_id):
    """Xử lý các task search cho tới khi hết queue, hết giờ, hoặc hết quota.

    Trả về dict trạng thái để supervisor báo cáo.
    """
    yt.reset_counters()
    seed_queue(store)
    store.kv_set("youtube_status", "running")
    store.kv_set("youtube_revisited_this_pass", False)
    processed_videos = set(store.kv_get("youtube_processed_videos", []))
    backlog_result = _drain_accepted_contexts(store, run, deadline_check, batch_id, processed_videos)
    if backlog_result:
        return backlog_result
    try:
        seed_deficit_refresh(store)
    except yt.YouTubeAPIError as e:
        if e.reason == "quotaExceeded":
            store.kv_set("youtube_status", "quota_wait")
            return {"status": "quota_wait"}
        if e.reason == "rateLimitExceeded":
            return _rate_limit_wait(store)
        store.log_event("yt_rank_error", "youtube", {"reason": e.reason})
        return {"status": "retry_wait", "retry_seconds": 60}
    except OSError as exc:
        store.log_event("yt_rank_error", "youtube", {"reason": repr(exc)[:200]})
        return {"status": "retry_wait", "retry_seconds": 60}

    while True:
        if deadline_check():
            store.kv_set("youtube_status", "deadline")
            return {"status": "deadline"}
        task = store.claim("youtube")
        if task is None:
            if not store.kv_get("youtube_revisited_this_pass"):
                _revisit_current_year(store)
                store.kv_set("youtube_revisited_this_pass", True)
                continue
            store.kv_set("youtube_status", "queue_empty")
            return {"status": "queue_empty"}
        store.kv_set("youtube_revisited_this_pass", False)

        payload = task["payload"]
        if task["source"] == "refresh_comments":
            video = _video_from_context(store, payload["video_id"])
            if video is None:
                store.complete_task(task["id"], "failed")
                continue
            before = store.conn.execute(
                "SELECT COUNT(*) n FROM comments WHERE platform='youtube' AND context_id=?",
                (video["video_id"],),
            ).fetchone()["n"]
            try:
                _crawl_video(store, video, run["run_id"], batch_id)
            except yt.YouTubeAPIError as e:
                if e.reason == "quotaExceeded":
                    store.defer_task(task["id"], delay=0)
                    store.kv_set("youtube_status", "quota_wait")
                    return {"status": "quota_wait"}
                if e.reason == "rateLimitExceeded":
                    return _rate_limit_wait(store, task["id"])
                terminal = {"commentsDisabled": "comments_disabled", "videoNotFound": "video_not_found"}
                if e.reason in terminal:
                    store.set_context_state("youtube", video["video_id"], terminal[e.reason])
                    store.complete_task(task["id"])
                    continue
                store.defer_task(task["id"], delay=120)
                store.log_event("yt_refresh_error", "youtube", {"video": video["video_id"], "reason": e.reason})
                return {"status": "retry_wait", "retry_seconds": 120}
            except OSError as exc:
                store.defer_task(task["id"], delay=60)
                store.log_event("yt_refresh_error", "youtube", {"video": video["video_id"], "reason": repr(exc)[:200]})
                return {"status": "retry_wait", "retry_seconds": 60}
            after = store.conn.execute(
                "SELECT COUNT(*) n FROM comments WHERE platform='youtube' AND context_id=?",
                (video["video_id"],),
            ).fetchone()["n"]
            added = after - before
            store.complete_task(task["id"])
            store.kv_set("youtube_rate_limit_failures", 0)
            if added > 0:
                store.reset_comment_retry_count("youtube", video["video_id"])
                store.set_context_state("youtube", video["video_id"], "comments_done")
                retry_count = 0
            else:
                retry_count = store.bump_comment_retry_count("youtube", video["video_id"])
            store.log_event("youtube_refresh_done", "youtube", {
                "video": video["video_id"], "deficit": payload["deficit"], "added": added,
                "retry_count": retry_count,
            })
            continue

        try:
            videos = yt.search_videos(payload["keyword"], payload["after"], payload["before"], max_pages=1, max_calls=None)
        except yt.YouTubeAPIError as e:
            if e.reason == "quotaExceeded":
                store.defer_task(task["id"], delay=0)  # trả lại queue, chưa xử lý.
                store.kv_set("youtube_status", "quota_wait")
                store.log_event("quota", "youtube", {"endpoint": "search"})
                return {"status": "quota_wait"}
            if e.reason == "rateLimitExceeded":
                return _rate_limit_wait(store, task["id"])
            store.retry_task(task["id"], delay=120)
            continue
        except OSError:
            store.retry_task(task["id"], delay=60)
            continue

        store.kv_set("youtube_rate_limit_failures", 0)
        new_ids = [v["video_id"] for v in videos if v["video_id"] not in processed_videos]
        full_descriptions = {}
        for offset in range(0, len(new_ids), 50):
            try:
                full_descriptions.update(yt.fetch_video_descriptions(new_ids[offset:offset + 50]))
            except yt.YouTubeAPIError as e:
                if e.reason == "quotaExceeded":
                    store.kv_set("youtube_processed_videos", list(processed_videos))
                    store.defer_task(task["id"], delay=0)
                    store.kv_set("youtube_status", "quota_wait")
                    return {"status": "quota_wait"}
                store.log_event("yt_desc_error", "youtube", {"reason": e.reason})
        for video in videos:
            if not post_in_window(video.get("published_at")):
                continue
            vid = video["video_id"]
            if vid in processed_videos:
                continue
            if vid in full_descriptions:
                video["description"] = full_descriptions[vid]
            verdict, reason = gate_post(video.get("title", ""), video.get("description", ""))
            store.upsert_context({
                "platform": "youtube", "context_id": vid,
                "source_url": f"https://www.youtube.com/watch?v={vid}",
                "post_title": video.get("title", ""), "post_context": f"{video.get('title','')}\n{video.get('description','')}".strip(),
                "post_published_at_raw": video.get("published_at", ""), "post_published_at": video.get("published_at", ""),
                "verdict": verdict, "reason": reason, "metadata_resolved": 1,
                "matched_groups": ",".join(matched_groups(video.get("title", "") + " " + video.get("description", ""))),
                "co_thanh_pho_khac": int(co_thanh_pho_khac(video.get("title", ""), video.get("description", ""))),
                "discovery_source": "search", "discovery_query": payload["keyword"],
                "state": "accepted" if verdict == "accept" else "rejected",
                "crawled_at": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat(),
            })
            if verdict != "accept":
                processed_videos.add(vid)
                continue
            try:
                _crawl_video(store, video, run["run_id"], batch_id)
                store.set_context_state("youtube", vid, "comments_done")
                processed_videos.add(vid)
            except yt.YouTubeAPIError as e:
                if e.reason == "quotaExceeded":
                    store.kv_set("youtube_processed_videos", list(processed_videos))
                    store.defer_task(task["id"], delay=0)
                    store.kv_set("youtube_status", "quota_wait")
                    return {"status": "quota_wait"}
                if e.reason not in ("commentsDisabled", "videoNotFound", "commentNotFound"):
                    store.log_event("yt_comment_error", "youtube", {"video": vid, "reason": e.reason})
            except OSError:
                time.sleep(5)

        store.complete_task(task["id"])
        store.kv_set("youtube_processed_videos", list(processed_videos))
