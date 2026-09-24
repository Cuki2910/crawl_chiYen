"""Tests baseline: store, YouTube worker records, Facebook worker records.

Không dùng network — mock API/browser. Chạy: python -m unittest tests.test_baseline -v
"""
import os
import sys
import tempfile
import shutil
import sqlite3
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("YOUTUBE_API_KEY", "test")
os.environ.setdefault("FB_HANDLE_SALT", "test-salt")

from crawlers.baseline_qc import check as qc_check
from crawlers.baseline_store import BaselineStore, DB_FILE, _SCHEMA

COMMENT_TIME = "Thứ bảy, 11 Tháng 7, 2026 lúc 17:43"
COMMENT_TIME_ISO = "2026-07-11T17:43:00+07:00"


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bt_")
        self.store = BaselineStore(base_dir=self.tmp)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_deadline_immutable_on_resume(self):
        r1 = self.store.get_or_create_run(24, 5000)
        r2 = self.store.get_or_create_run(24, 5000)
        self.assertEqual(r1["deadline_at"], r2["deadline_at"])
        self.assertEqual(r1["run_id"], r2["run_id"])

    def test_enqueue_dedup_and_claim(self):
        self.assertTrue(self.store.enqueue("youtube", "search", {"q": "a"}))
        self.assertFalse(self.store.enqueue("youtube", "search", {"q": "a"}))
        t = self.store.claim("youtube")
        self.assertIsNotNone(t)
        self.assertIsNone(self.store.claim("youtube"))  # đã lease.
        self.store.complete_task(t["id"])

    def test_comment_dedup_and_empty(self):
        run = self.store.get_or_create_run(24, 5000)
        self._accept_context("youtube", "v")
        base = {
            "platform": "youtube", "comment_id": "c1", "context_id": "v", "id": "youtube_c_c1",
            "source_url": "u", "post_context": "", "post_title": "", "post_published_at_raw": "",
            "post_published_at": "", "comment_text": "hi", "comment_type": "top_level",
            "parent_comment_id": None, "thread_id": "c1", "reply_depth": 0, "posted_at_raw": "",
            "likes_count": 0, "author_key": None, "matched_groups": "", "co_thanh_pho_khac": 0,
            "parent_unresolved": 0, "crawl_batch_id": "b", "run_id": run["run_id"], "crawled_at": "n",
        }
        self.assertEqual(self.store.add_comments([base]), 1)
        self.assertEqual(self.store.add_comments([base]), 0)
        self.assertEqual(self.store.add_comments([dict(base, comment_id="c2", comment_text="  ")]), 0)
        self.assertEqual(self.store.comment_count("youtube"), 1)

    def test_facebook_store_rejects_missing_timestamp(self):
        self._accept_context("facebook", "v")
        base = {
            "platform": "facebook", "comment_id": "c1", "context_id": "v",
            "comment_text": "hi", "comment_type": "top_level",
        }
        self.assertEqual(self.store.add_comments([base]), 0)
        invalid = dict(base, posted_at_raw="2 tuần trước", posted_at=COMMENT_TIME_ISO)
        self.assertEqual(self.store.add_comments([invalid]), 0)
        valid = dict(base, posted_at_raw=COMMENT_TIME, posted_at=COMMENT_TIME_ISO)
        self.assertEqual(self.store.add_comments([valid]), 1)

    def test_facebook_recrawl_repairs_legacy_timestamp(self):
        self._accept_context("facebook", "v")
        repaired = {
            "platform": "facebook", "context_id": "v", "comment_text": "new",
            "comment_type": "top_level", "posted_at_raw": COMMENT_TIME,
            "posted_at": COMMENT_TIME_ISO,
        }
        for comment_id, old_iso in (("missing", None), ("fake", COMMENT_TIME_ISO)):
            with self.subTest(comment_id=comment_id):
                self.store.conn.execute(
                    "INSERT INTO comments (platform,comment_id,context_id,comment_text,comment_type,posted_at_raw,posted_at) VALUES (?,?,?,?,?,?,?)",
                    ("facebook", comment_id, "v", "old", "top_level", "2 tuần trước", old_iso),
                )
                self.assertEqual(
                    self.store.conn.execute(
                        "SELECT facebook_comment_time_valid(posted_at_raw,posted_at) valid FROM comments WHERE comment_id=?",
                        (comment_id,),
                    ).fetchone()["valid"],
                    0,
                )
                self.assertEqual(self.store.add_comments([dict(repaired, comment_id=comment_id)]), 1)
        rows = self.store.conn.execute(
            "SELECT comment_text,posted_at_raw,posted_at FROM comments WHERE platform='facebook' ORDER BY comment_id"
        ).fetchall()
        self.assertEqual([dict(row) for row in rows], [{
            "comment_text": "new", "posted_at_raw": COMMENT_TIME, "posted_at": COMMENT_TIME_ISO,
        }] * 2)
        self.assertEqual(self.store.comment_count("facebook"), 2)
        self.assertEqual(self.store.export_csv()["facebook_comments"][1], 2)

    def test_retry_marks_failed_after_max(self):
        self.store.enqueue("facebook", "discover", {"x": 1})
        t = self.store.claim("facebook")
        for _ in range(6):
            self.store.retry_task(t["id"], delay=0, max_attempts=5)
            claimed = self.store.claim("facebook")
            if claimed:
                t = claimed
        # cuối cùng phải chuyển failed, không claim được nữa.
        self.assertIsNone(self.store.claim("facebook"))

    def test_existing_database_adds_posted_at_without_backfill(self):
        self.store.close()
        os.remove(os.path.join(self.tmp, DB_FILE))
        conn = sqlite3.connect(os.path.join(self.tmp, DB_FILE))
        legacy_schema = _SCHEMA.replace("    posted_at TEXT,\n", "", 1)
        conn.executescript(legacy_schema)
        conn.execute(
            "INSERT INTO comments (platform,comment_id,context_id,comment_text,comment_type,posted_at_raw) VALUES (?,?,?,?,?,?)",
            ("facebook", "legacy", "v", "old", "top_level", "2 tuần trước"),
        )
        conn.commit()
        conn.close()

        self.store = BaselineStore(base_dir=self.tmp)
        columns = {row["name"] for row in self.store.conn.execute("PRAGMA table_info(comments)")}
        self.assertIn("posted_at", columns)
        row = self.store.conn.execute("SELECT posted_at FROM comments WHERE comment_id='legacy'").fetchone()
        self.assertIsNone(row["posted_at"])
        self.assertEqual(self.store.comment_count("facebook"), 0)
        self.assertEqual(self.store.export_csv()["facebook_comments"][1], 0)

    def test_qc_rejects_invalid_facebook_timestamp(self):
        self.store.conn.execute(
            "INSERT INTO comments (platform,comment_id,context_id,comment_text,comment_type,posted_at_raw,posted_at) VALUES (?,?,?,?,?,?,?)",
            ("facebook", "bad", "v", "bad", "top_level", "2 tuần trước", "2026-07-11T17:43:00+07:00"),
        )
        result = qc_check(self.store, sample_size=0)
        self.assertFalse(result["ok"])
        self.assertIn("facebook_invalid_comment_timestamp", {issue["kind"] for issue in result["issues"]})

    def _accept_context(self, platform, context_id):
        self.store.upsert_context({
            "platform": platform, "context_id": context_id, "source_url": "u", "post_title": "t",
            "post_context": "c", "post_published_at_raw": "", "post_published_at": "",
            "verdict": "accept", "reason": "ok", "metadata_resolved": 1, "matched_groups": "",
            "co_thanh_pho_khac": 0, "discovery_source": "s", "discovery_query": "q",
            "state": "accepted", "crawled_at": "n",
        })

    def test_add_comments_rejects_missing_context(self):
        base = {
            "platform": "youtube", "comment_id": "c1", "context_id": "missing", "comment_text": "hi",
            "comment_type": "top_level",
        }
        self.assertEqual(self.store.add_comments([base]), 0)
        self.assertEqual(self.store.comment_count("youtube"), 0)

    def test_add_comments_rejects_non_accepted_context(self):
        self.store.upsert_context({
            "platform": "youtube", "context_id": "v", "source_url": "u", "post_title": "t",
            "post_context": "c", "post_published_at_raw": "", "post_published_at": "",
            "verdict": "reject", "reason": "no", "metadata_resolved": 1, "matched_groups": "",
            "co_thanh_pho_khac": 0, "discovery_source": "s", "discovery_query": "q",
            "state": "rejected", "crawled_at": "n",
        })
        base = {
            "platform": "youtube", "comment_id": "c1", "context_id": "v", "comment_text": "hi",
            "comment_type": "top_level",
        }
        self.assertEqual(self.store.add_comments([base]), 0)

    def test_upsert_context_blocks_downgrade_with_comments(self):
        for platform in ("youtube", "facebook"):
            with self.subTest(platform=platform):
                self._accept_context(platform, "v")
                self.store.add_comments([{
                    "platform": platform, "comment_id": "c1", "context_id": "v", "comment_text": "hi",
                    "comment_type": "top_level",
                    **({"posted_at_raw": COMMENT_TIME, "posted_at": COMMENT_TIME_ISO} if platform == "facebook" else {}),
                }])
                self.assertEqual(self.store.comment_count(platform), 1)
                ok = self.store.upsert_context({
                    "platform": platform, "context_id": "v", "source_url": "u", "post_title": "t2",
                    "post_context": "c2", "post_published_at_raw": "", "post_published_at": "",
                    "verdict": "reject", "reason": "regate", "metadata_resolved": 1, "matched_groups": "",
                    "co_thanh_pho_khac": 0, "discovery_source": "s", "discovery_query": "q",
                    "state": "rejected", "crawled_at": "n2",
                })
                self.assertFalse(ok)
                ctx = self.store.context(platform, "v")
                self.assertEqual(ctx["verdict"], "accept")
                self.assertEqual(ctx["post_title"], "t")
                self.assertEqual(self.store.comment_count(platform), 1)
                event = self.store.conn.execute(
                    "SELECT kind FROM events WHERE kind='context_downgrade_blocked' AND platform=?",
                    (platform,),
                ).fetchone()
                self.assertIsNotNone(event)
                result = qc_check(self.store, sample_size=0)
                self.assertTrue(result["ok"], result["issues"])

    def test_upsert_context_allows_downgrade_without_comments(self):
        self._accept_context("youtube", "v")
        ok = self.store.upsert_context({
            "platform": "youtube", "context_id": "v", "source_url": "u", "post_title": "t2",
            "post_context": "c2", "post_published_at_raw": "", "post_published_at": "",
            "verdict": "reject", "reason": "regate", "metadata_resolved": 1, "matched_groups": "",
            "co_thanh_pho_khac": 0, "discovery_source": "s", "discovery_query": "q",
            "state": "rejected", "crawled_at": "n2",
        })
        self.assertTrue(ok)
        self.assertEqual(self.store.context("youtube", "v")["verdict"], "reject")

    def test_export_roundtrip(self):
        run = self.store.get_or_create_run(24, 5000)
        self.store.upsert_context({
            "platform": "youtube", "context_id": "v", "source_url": "u", "post_title": "t",
            "post_context": "c", "post_published_at_raw": "", "post_published_at": "",
            "verdict": "accept", "reason": "ok", "metadata_resolved": 1, "matched_groups": "",
            "co_thanh_pho_khac": 0, "discovery_source": "s", "discovery_query": "q",
            "state": "accepted", "crawled_at": "n",
        })
        self.store.add_comments([{
            "platform": "youtube", "comment_id": "c1", "context_id": "v", "id": "youtube_c_c1",
            "source_url": "u", "post_context": "c", "post_title": "t", "post_published_at_raw": "",
            "post_published_at": "", "comment_text": "hi", "comment_type": "top_level",
            "parent_comment_id": None, "thread_id": "c1", "reply_depth": 0, "posted_at_raw": "",
            "likes_count": 0, "author_key": None, "matched_groups": "", "co_thanh_pho_khac": 0,
            "parent_unresolved": 0, "crawl_batch_id": "b", "run_id": run["run_id"], "crawled_at": "n",
        }])
        outs = self.store.export_csv()
        self.assertEqual(outs["youtube_comments"][1], 1)
        self.assertTrue(os.path.exists(outs["youtube_comments"][0]))


class YouTubeWorkerTest(unittest.TestCase):
    def test_record_types_and_ids(self):
        from crawlers.youtube import youtube_worker as w
        video = {"video_id": "vid1", "title": "xe buýt miễn phí Sài Gòn", "description": "TP.HCM", "published_at": "2026"}
        top = w._record(video, {"textDisplay": "hay", "publishedAt": "t", "likeCount": 2}, "t1", "top_level", None, "t1", 0, "run", "b")
        rep = w._record(video, {"textDisplay": "ok", "publishedAt": "t", "likeCount": 0}, "r1", "reply", "t1", "t1", 1, "run", "b")
        self.assertEqual(top["id"], "youtube_c_t1")
        self.assertEqual(top["comment_type"], "top_level")
        self.assertEqual(rep["parent_comment_id"], "t1")
        self.assertEqual(rep["reply_depth"], 1)

    def test_windows_desc_order(self):
        from crawlers.youtube import youtube_worker as w
        wins = w._windows()
        self.assertTrue(all(a < b for a, b in wins))
        self.assertEqual(len(wins), 2)


class FacebookWorkerTest(unittest.TestCase):
    def test_records_parent_unresolved(self):
        from crawlers.facebook import facebook_worker as w
        recs = w._to_records(None, "https://www.facebook.com/gtcctphcm/posts/123",
                             "xe buýt miễn phí TP.HCM", {"post_published_at_raw": "", "post_published_at": ""},
                             [{"comment_text": "tốt", "posted_at_raw": COMMENT_TIME}], "run", "b")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["parent_unresolved"], 0)
        self.assertEqual(recs[0]["comment_type"], "top_level")
        self.assertTrue(recs[0]["id"].startswith("facebook_c_"))

    def test_records_preserve_reply_hierarchy(self):
        from crawlers.facebook import facebook_worker as w
        comments = [
            {"comment_text": "reply", "comment_id": "r1", "comment_type": "reply",
             "parent_comment_id": "p1", "posted_at_raw": COMMENT_TIME},
            {"comment_text": "unresolved", "comment_id": "r2", "comment_type": "reply",
             "parent_unresolved": True, "posted_at_raw": COMMENT_TIME},
        ]
        recs = w._to_records(None, "https://www.facebook.com/gtcctphcm/posts/123",
                             "xe buýt miễn phí TP.HCM", {"post_published_at_raw": "", "post_published_at": ""},
                             comments, "run", "b")
        self.assertEqual(recs[0]["comment_type"], "reply")
        self.assertEqual(recs[0]["parent_comment_id"], "p1")
        self.assertEqual(recs[0]["thread_id"], "p1")
        self.assertEqual(recs[0]["reply_depth"], 1)
        self.assertEqual(recs[0]["parent_unresolved"], 0)
        self.assertEqual(recs[1]["comment_type"], "reply")
        self.assertIsNone(recs[1]["parent_comment_id"])
        self.assertEqual(recs[1]["parent_unresolved"], 1)

    def test_contamination_rejects_giant_text(self):
        from crawlers.facebook import facebook_worker as w
        url = "https://www.facebook.com/gtcctphcm/posts/123"
        self.assertTrue(w._contamination_ok(url, [{"comment_text": "ngắn", "post_url": url}]))
        self.assertFalse(w._contamination_ok(url, [{"comment_text": "x" * 9000, "post_url": url}]))
        self.assertFalse(w._contamination_ok(url, [{"comment_text": "ngắn"}]))
        self.assertFalse(w._contamination_ok(url, [{"comment_text": "ngắn", "post_url": "https://www.facebook.com/other/posts/456"}]))

    def test_combo_sources_use_approved_keywords(self):
        from crawlers.facebook import facebook_worker as w
        from crawlers.facebook import crawl_facebook as fb
        self.assertEqual(w._combo_sources(), [])
        self.assertTrue(fb.SOURCES)
        self.assertTrue(all("query" in s for s in fb.SOURCES))
        self.assertTrue(all(s["source_class"] == "topic_search" for s in fb.SOURCES))


if __name__ == "__main__":
    unittest.main()
