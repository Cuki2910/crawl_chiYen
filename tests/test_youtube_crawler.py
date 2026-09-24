import importlib
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("YOUTUBE_API_KEY", "test")
yt = importlib.import_module("crawlers.youtube.crawl_youtube")


class YouTubeCrawlerTests(unittest.TestCase):
    def test_search_keeps_metadata(self):
        payload = {"items": [{"id": {"videoId": "v1"}, "snippet": {"title": "Xe buýt", "description": "miễn phí HCM", "publishedAt": "2026-01-01T00:00:00Z"}}]}
        with patch.object(yt, "_api_get", return_value=payload), patch.object(yt.time, "sleep"):
            yt.reset_counters()
            video = yt.search_videos("q", "2025-01-01T00:00:00Z", "2027-01-01T00:00:00Z", max_pages=1)[0]
        self.assertEqual(video["description"], "miễn phí HCM")
        self.assertEqual(video["published_at"], "2026-01-01T00:00:00Z")

    def test_worker_record_keeps_post_metadata_and_comment_labels(self):
        from crawlers.youtube import youtube_worker

        video = {"video_id": "v1", "title": "Hà Nội ngập đường", "description": "xe buýt chậm", "published_at": "2026-08-01T00:00:00Z"}
        snippet = {"textDisplay": "Hà Nội ngập đường, xe buýt chậm", "publishedAt": "2026-01-02T00:00:00Z", "likeCount": 1}
        record = youtube_worker._record(video, snippet, "c1", "top_level", None, "c1", 0, "run", "batch")
        self.assertEqual(record["post_context"], "Hà Nội ngập đường\nxe buýt chậm")
        self.assertEqual(record["posted_at"], "2026-01-02T00:00:00Z")
        self.assertIn("flood_state", record["matched_groups"])
        self.assertIn("transport_impact", record["matched_groups"])

    def test_fetch_video_comment_counts_batches_statistics(self):
        payload = {"items": [
            {"id": "v1", "statistics": {"commentCount": "123"}},
            {"id": "v2", "statistics": {}},
        ]}
        with patch.object(yt, "_api_get", return_value=payload) as api:
            counts = yt.fetch_video_comment_counts(["v1", "v2"])
        self.assertEqual(counts, {"v1": 123, "v2": 0})
        self.assertEqual(api.call_args.args[0], "videos")
        with self.assertRaises(ValueError):
            yt.fetch_video_comment_counts([str(i) for i in range(51)])

    def test_fetch_video_descriptions_returns_full_text(self):
        payload = {"items": [
            {"id": "v1", "snippet": {"description": "mô tả đầy đủ không bị cắt " * 5}},
        ]}
        with patch.object(yt, "_api_get", return_value=payload) as api:
            descs = yt.fetch_video_descriptions(["v1"])
        self.assertEqual(descs["v1"], "mô tả đầy đủ không bị cắt " * 5)
        self.assertEqual(api.call_args.args[0], "videos")
        self.assertEqual(api.call_args.args[1]["part"], "snippet")
        with self.assertRaises(ValueError):
            yt.fetch_video_descriptions([str(i) for i in range(51)])

    def test_backfill_regates_with_full_description_and_resumes(self):
        import sqlite3
        from crawlers.baseline_store import BaselineStore
        from crawlers.youtube import backfill_descriptions as bf

        legacy_dir = tempfile.mkdtemp(prefix="yt_legacy_")
        output_dir = tempfile.mkdtemp(prefix="yt_backfill_")
        try:
            legacy_db = os.path.join(legacy_dir, "crawl.db")
            conn = sqlite3.connect(legacy_db)
            conn.execute(
                "CREATE TABLE contexts (platform TEXT, context_id TEXT, post_title TEXT, "
                "post_published_at_raw TEXT, post_published_at TEXT)"
            )
            # v1: title thiếu nhóm B nhưng description đầy đủ (bị search.list cắt mất phần đó) có nhóm B -> đổi accept.
            # v2: ngay cả description đầy đủ vẫn thiếu nhóm C -> vẫn reject.
            conn.execute("INSERT INTO contexts VALUES ('youtube','v1','Hà Nội ngập','t','2026-08-01T00:00:00Z')")
            conn.execute("INSERT INTO contexts VALUES ('youtube','v2','Hà Nội xe buýt','t','2026-08-01T00:00:00Z')")
            conn.commit()
            conn.close()

            full_desc = {"v1": "giao thông xe buýt bị chậm", "v2": "giao thông bình thường"}
            with patch.object(bf.yt, "fetch_video_descriptions", return_value=full_desc), \
                 patch.object(bf, "_crawl_video", return_value=3) as crawl_mock:
                bf.run_backfill(legacy_db, batch_size=50, base_dir=output_dir)

            store = BaselineStore(base_dir=output_dir)
            v1 = store.conn.execute("SELECT * FROM contexts WHERE context_id='v1'").fetchone()
            v2 = store.conn.execute("SELECT * FROM contexts WHERE context_id='v2'").fetchone()
            self.assertEqual(v1["verdict"], "accept")
            self.assertEqual(v1["state"], "comments_done")
            self.assertIn("giao thông xe buýt", v1["post_context"])
            self.assertEqual(v2["verdict"], "reject")
            self.assertEqual(crawl_mock.call_count, 1)  # chỉ video accept mới crawl comment.
            done_ids = set(store.kv_get("youtube_backfill_done_ids", []))
            self.assertEqual(done_ids, {"v1", "v2"})
            store.close()

            # Chạy lại: không còn video pending -> không gọi lại API.
            with patch.object(bf.yt, "fetch_video_descriptions") as api_mock, \
                 patch.object(bf, "_crawl_video"):
                bf.run_backfill(legacy_db, batch_size=50, base_dir=output_dir)
            api_mock.assert_not_called()
        finally:
            shutil.rmtree(legacy_dir, ignore_errors=True)
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_deficit_refresh_ranks_largest_first(self):
        from crawlers.baseline_store import BaselineStore
        from crawlers.youtube import youtube_worker

        output_dir = tempfile.mkdtemp(prefix="yt_deficit_")
        store = BaselineStore(base_dir=output_dir)
        try:
            run = store.get_or_create_run(24, 5000)
            for video_id in ("v1", "v2"):
                store.upsert_context({
                    "platform": "youtube", "context_id": video_id, "source_url": "u",
                    "post_title": "t", "post_context": "t\nd", "post_published_at_raw": "t",
                    "post_published_at": "t", "verdict": "accept", "reason": "du_3_nhom",
                    "metadata_resolved": 1, "matched_groups": "", "co_thanh_pho_khac": 0,
                    "discovery_source": "search", "discovery_query": "q", "state": "comments_done",
                    "crawled_at": "now",
                })
            with patch.object(yt, "fetch_video_comment_counts", return_value={"v1": 10, "v2": 100}):
                self.assertEqual(youtube_worker.seed_deficit_refresh(store), 2)
            tasks = store.conn.execute(
                "SELECT payload,priority FROM discovery_queue WHERE source='refresh_comments' ORDER BY priority,id"
            ).fetchall()
            self.assertIn('"video_id": "v2"', tasks[0]["payload"])
            self.assertEqual(tasks[0]["priority"], -100)
            self.assertEqual(tasks[1]["priority"], -10)
        finally:
            store.close()
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_refresh_task_is_checkpointed_and_deduped(self):
        from crawlers.baseline_store import BaselineStore
        from crawlers.youtube import youtube_worker

        output_dir = tempfile.mkdtemp(prefix="yt_refresh_")
        store = BaselineStore(base_dir=output_dir)
        try:
            run = store.get_or_create_run(24, 5000)
            store.kv_set("youtube_seeded", True)
            store.kv_set("youtube_deficit_ranked_hour", time.strftime("%Y%m%d%H", time.gmtime()))
            store.upsert_context({
                "platform": "youtube", "context_id": "v1", "source_url": "u",
                "post_title": "t", "post_context": "t\nd", "post_published_at_raw": "t",
                "post_published_at": "t", "verdict": "accept", "reason": "du_3_nhom",
                "metadata_resolved": 1, "matched_groups": "", "co_thanh_pho_khac": 0,
                "discovery_source": "search", "discovery_query": "q", "state": "comments_done",
                "crawled_at": "now",
            })
            store.enqueue("youtube", "refresh_comments", {
                "video_id": "v1", "comment_count": 10, "stored_count": 0, "deficit": 10,
            }, priority=-10)
            with patch.object(youtube_worker, "_crawl_video", return_value=0), \
                 patch.object(youtube_worker, "_revisit_current_year"):
                result = youtube_worker.run_once(store, run, lambda: False, "batch")
            self.assertEqual(result["status"], "queue_empty")
            task = store.conn.execute("SELECT status FROM discovery_queue WHERE source='refresh_comments'").fetchone()
            self.assertEqual(task["status"], "done")
        finally:
            store.close()
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_deficit_refresh_excludes_video_after_max_retries(self):
        from crawlers.baseline_store import BaselineStore
        from crawlers.youtube import youtube_worker

        output_dir = tempfile.mkdtemp(prefix="yt_deficit_stuck_")
        store = BaselineStore(base_dir=output_dir)
        try:
            store.upsert_context({
                "platform": "youtube", "context_id": "v1", "source_url": "u",
                "post_title": "t", "post_context": "t\nd", "post_published_at_raw": "t",
                "post_published_at": "t", "verdict": "accept", "reason": "du_3_nhom",
                "metadata_resolved": 1, "matched_groups": "", "co_thanh_pho_khac": 0,
                "discovery_source": "search", "discovery_query": "q", "state": "comments_done",
                "crawled_at": "now", "comment_retry_count": youtube_worker._MAX_REFRESH_RETRIES,
            })
            with patch.object(yt, "fetch_video_comment_counts", return_value={"v1": 999}):
                queued = youtube_worker.seed_deficit_refresh(store)
            self.assertEqual(queued, 0)
        finally:
            store.close()
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_refresh_zero_added_bumps_retry_count(self):
        from crawlers.baseline_store import BaselineStore
        from crawlers.youtube import youtube_worker

        output_dir = tempfile.mkdtemp(prefix="yt_refresh_zero_")
        store = BaselineStore(base_dir=output_dir)
        try:
            run = store.get_or_create_run(24, 5000)
            store.kv_set("youtube_seeded", True)
            store.kv_set("youtube_deficit_ranked_hour", time.strftime("%Y%m%d%H", time.gmtime()))
            store.upsert_context({
                "platform": "youtube", "context_id": "v1", "source_url": "u",
                "post_title": "t", "post_context": "t\nd", "post_published_at_raw": "t",
                "post_published_at": "t", "verdict": "accept", "reason": "du_3_nhom",
                "metadata_resolved": 1, "matched_groups": "", "co_thanh_pho_khac": 0,
                "discovery_source": "search", "discovery_query": "q", "state": "comments_done",
                "crawled_at": "now",
            })
            store.enqueue("youtube", "refresh_comments", {
                "video_id": "v1", "comment_count": 10, "stored_count": 0, "deficit": 10,
            }, priority=-10)
            with patch.object(youtube_worker, "_crawl_video", return_value=0), \
                 patch.object(youtube_worker, "_revisit_current_year"):
                youtube_worker.run_once(store, run, lambda: False, "batch")
            self.assertEqual(store.context("youtube", "v1")["comment_retry_count"], 1)
        finally:
            store.close()
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_api_get_reclassifies_daily_search_quota_as_quota_exceeded(self):
        import io
        import json as json_module
        from urllib.error import HTTPError

        body = json_module.dumps({"error": {"errors": [{
            "reason": "rateLimitExceeded",
            "message": "Quota exceeded for quota metric 'Search Queries' and limit "
                       "'Search Queries per day' of service 'youtube.googleapis.com'.",
        }]}}).encode()
        http_error = HTTPError("url", 429, "Too Many Requests", {}, io.BytesIO(body))
        with patch.object(yt, "_API_KEYS", ["key1"]), \
             patch.object(yt.urllib.request, "urlopen", side_effect=http_error), \
             patch.object(yt.time, "sleep"):
            with self.assertRaises(yt.YouTubeAPIError) as raised:
                yt._api_get("search", {})
        self.assertEqual(raised.exception.reason, "quotaExceeded")

    def test_api_get_rotates_to_next_key_when_daily_quota_is_exhausted(self):
        import io
        import json as json_module
        from urllib.error import HTTPError

        body = json_module.dumps({"error": {"errors": [{
            "reason": "rateLimitExceeded",
            "message": "Quota exceeded for Search Queries per day",
        }]}}).encode()
        http_error = HTTPError("url", 429, "Too Many Requests", {}, io.BytesIO(body))
        success = MagicMock()
        success.__enter__.return_value.read.return_value = b'{"items": []}'
        seen_urls = []

        def urlopen(req):
            seen_urls.append(req.full_url)
            if len(seen_urls) == 1:
                raise http_error
            return success

        with patch.object(yt, "_API_KEYS", ["key1", "key2"]), \
             patch.object(yt, "_key_index", 0), \
             patch.object(yt.urllib.request, "urlopen", side_effect=urlopen), \
             patch.object(yt.time, "sleep"):
            self.assertEqual(yt._api_get("search", {}), {"items": []})
        self.assertIn("key=key1", seen_urls[0])
        self.assertIn("key=key2", seen_urls[1])

    def test_api_get_keeps_burst_rate_limit_as_is(self):
        import io
        import json as json_module
        from urllib.error import HTTPError

        body = json_module.dumps({"error": {"errors": [{
            "reason": "rateLimitExceeded",
            "message": "User Rate Limit Exceeded",
        }]}}).encode()
        http_error = HTTPError("url", 403, "Forbidden", {}, io.BytesIO(body))
        with patch.object(yt.urllib.request, "urlopen", side_effect=http_error), \
             patch.object(yt.time, "sleep"):
            with self.assertRaises(yt.YouTubeAPIError) as raised:
                yt._api_get("commentThreads", {})
        self.assertEqual(raised.exception.reason, "rateLimitExceeded")

    def test_search_propagates_rate_limit(self):
        error = yt.YouTubeAPIError(403, "rateLimitExceeded", {})
        with patch.object(yt, "_api_get", side_effect=error):
            with self.assertRaises(yt.YouTubeAPIError) as raised:
                yt.search_videos("q", "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z", max_pages=1)
        self.assertEqual(raised.exception.reason, "rateLimitExceeded")

    def test_worker_defers_rate_limited_task(self):
        from crawlers.baseline_store import BaselineStore
        from crawlers.youtube import youtube_worker

        output_dir = tempfile.mkdtemp(prefix="yt_rate_limit_")
        store = BaselineStore(base_dir=output_dir)
        try:
            run = store.get_or_create_run(24, 5000)
            store.kv_set("youtube_seeded", True)
            store.enqueue("youtube", "search", {
                "keyword": "q", "after": "2026-01-01T00:00:00Z", "before": "2027-01-01T00:00:00Z",
            })
            error = yt.YouTubeAPIError(403, "rateLimitExceeded", {})
            with patch.object(yt, "search_videos", side_effect=error):
                result = youtube_worker.run_once(store, run, lambda: False, "batch")

            task = store.conn.execute(
                "SELECT status, next_retry_at FROM discovery_queue WHERE platform='youtube'"
            ).fetchone()
            self.assertEqual(result["status"], "rate_limit_wait")
            self.assertEqual(task["status"], "pending")
            self.assertGreater(task["next_retry_at"], time.time() + 850)
        finally:
            store.close()
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_rate_limit_backoff_escalates_and_resets(self):
        from crawlers.baseline_store import BaselineStore
        from crawlers.youtube import youtube_worker

        output_dir = tempfile.mkdtemp(prefix="yt_backoff_")
        store = BaselineStore(base_dir=output_dir)
        try:
            run = store.get_or_create_run(24, 5000)
            store.kv_set("youtube_seeded", True)
            error = yt.YouTubeAPIError(403, "rateLimitExceeded", {})
            delays = []
            for index in range(3):
                store.enqueue("youtube", "search", {
                    "keyword": f"q{index}", "after": "2026-01-01T00:00:00Z", "before": "2027-01-01T00:00:00Z",
                })
                with patch.object(yt, "search_videos", side_effect=error):
                    delays.append(youtube_worker.run_once(store, run, lambda: False, "batch")["retry_seconds"])
            self.assertEqual(delays, [900, 1800, 3600])

            pending = store.conn.execute(
                "SELECT id FROM discovery_queue WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            store.conn.execute("UPDATE discovery_queue SET next_retry_at=0 WHERE id=?", (pending["id"],))
            store.conn.commit()
            with patch.object(yt, "search_videos", return_value=[]):
                youtube_worker.run_once(store, run, lambda: False, "batch")
            self.assertEqual(store.kv_get("youtube_rate_limit_failures"), 0)
        finally:
            store.close()
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_accepted_backlog_finishes_without_search(self):
        from crawlers.baseline_store import BaselineStore
        from crawlers.youtube import youtube_worker

        output_dir = tempfile.mkdtemp(prefix="yt_backlog_")
        store = BaselineStore(base_dir=output_dir)
        try:
            run = store.get_or_create_run(24, 5000)
            store.upsert_context({
                "platform": "youtube", "context_id": "v1", "source_url": "u",
                "post_title": "xe buýt miễn phí TP.HCM", "post_context": "xe buýt miễn phí TP.HCM\nmô tả",
                "post_published_at_raw": "t", "post_published_at": "t", "verdict": "accept",
                "reason": "du_3_nhom", "metadata_resolved": 1, "matched_groups": "",
                "co_thanh_pho_khac": 0, "discovery_source": "search", "discovery_query": "q",
                "state": "accepted", "crawled_at": "now",
            })
            with patch.object(youtube_worker, "_crawl_video", return_value=3):
                result = youtube_worker._drain_accepted_contexts(store, run, lambda: False, "batch", set())
            self.assertIsNone(result)
            self.assertEqual(store.context_state("youtube", "v1"), "comments_done")
        finally:
            store.close()
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_accepted_backlog_preserves_retryable_rate_limit(self):
        from crawlers.baseline_store import BaselineStore
        from crawlers.youtube import youtube_worker

        output_dir = tempfile.mkdtemp(prefix="yt_backlog_limit_")
        store = BaselineStore(base_dir=output_dir)
        try:
            run = store.get_or_create_run(24, 5000)
            store.upsert_context({
                "platform": "youtube", "context_id": "v1", "source_url": "u",
                "post_title": "t", "post_context": "t\nd", "post_published_at_raw": "t",
                "post_published_at": "t", "verdict": "accept", "reason": "du_3_nhom",
                "metadata_resolved": 1, "matched_groups": "", "co_thanh_pho_khac": 0,
                "discovery_source": "search", "discovery_query": "q", "state": "accepted",
                "crawled_at": "now",
            })
            error = yt.YouTubeAPIError(429, "rateLimitExceeded", {})
            with patch.object(youtube_worker, "_crawl_video", side_effect=error):
                result = youtube_worker._drain_accepted_contexts(store, run, lambda: False, "batch", set())
            self.assertEqual(result["status"], "rate_limit_wait")
            self.assertEqual(store.context_state("youtube", "v1"), "accepted")
        finally:
            store.close()
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_accepted_backlog_records_comments_disabled(self):
        from crawlers.baseline_store import BaselineStore
        from crawlers.youtube import youtube_worker

        output_dir = tempfile.mkdtemp(prefix="yt_backlog_disabled_")
        store = BaselineStore(base_dir=output_dir)
        try:
            run = store.get_or_create_run(24, 5000)
            store.upsert_context({
                "platform": "youtube", "context_id": "v1", "source_url": "u",
                "post_title": "t", "post_context": "t\nd", "post_published_at_raw": "t",
                "post_published_at": "t", "verdict": "accept", "reason": "du_3_nhom",
                "metadata_resolved": 1, "matched_groups": "", "co_thanh_pho_khac": 0,
                "discovery_source": "search", "discovery_query": "q", "state": "accepted",
                "crawled_at": "now",
            })
            error = yt.YouTubeAPIError(403, "commentsDisabled", {})
            with patch.object(youtube_worker, "_crawl_video", side_effect=error):
                result = youtube_worker._drain_accepted_contexts(store, run, lambda: False, "batch", set())
            self.assertIsNone(result)
            self.assertEqual(store.context_state("youtube", "v1"), "comments_disabled")
        finally:
            store.close()
            shutil.rmtree(output_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
