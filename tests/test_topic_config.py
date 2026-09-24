import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from crawlers.hanoi_flood_transport.config import post_in_window, youtube_windows


class TopicConfigTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
