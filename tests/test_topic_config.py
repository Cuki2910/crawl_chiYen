import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from crawlers.hanoi_flood_transport.config import post_in_window, youtube_windows
from crawlers.topics import gate_post


class TopicConfigTests(unittest.TestCase):
    def test_crawlers_share_approved_hanoi_queries(self):
        from crawlers.facebook import crawl_facebook as facebook
        from crawlers.facebook.facebook_smoke import QUERY as smoke_query
        from crawlers.hanoi_flood_transport.config import TOPIC_QUERIES
        from crawlers.youtube import crawl_youtube as youtube

        self.assertEqual(youtube.KEYWORDS, list(TOPIC_QUERIES))
        self.assertEqual(len(facebook.SOURCES), len(TOPIC_QUERIES) * 2)
        self.assertTrue(all(source["query"] in TOPIC_QUERIES for source in facebook.SOURCES))
        self.assertEqual(smoke_query, TOPIC_QUERIES[0])
        for marker in ("TP.HCM", "Sài Gòn", "VNeID", "MultiGo", "Thủ Đức"):
            self.assertNotIn(marker, TOPIC_QUERIES)

    def test_publication_windows_and_boundaries(self):
        hcm = ZoneInfo("Asia/Ho_Chi_Minh")
        now = datetime(2026, 9, 24, 8, 0, tzinfo=hcm)
        self.assertTrue(post_in_window("2025-08-01T00:00:00", now))
        self.assertFalse(post_in_window("2025-11-01T00:00:00", now))
        self.assertTrue(post_in_window("2026-09-24T07:59:59", now))
        self.assertFalse(post_in_window("2026-09-24T08:00:00", now))
        self.assertEqual(
            list(youtube_windows(now)),
            [
                ("2025-07-31T17:00:00Z", "2025-10-31T17:00:00Z"),
                ("2026-07-31T17:00:00Z", "2026-09-24T01:00:00Z"),
            ],
        )

    def test_gate_rejects_non_flood_ngap_tran_text(self):
        verdict, reason = gate_post("", "Hà Nội ngập tràn niềm vui đi đón cha")
        self.assertEqual((verdict, reason), ("reject", "missing_group:flood_state"))


if __name__ == "__main__":
    unittest.main()
