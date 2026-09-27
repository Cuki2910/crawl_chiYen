import unittest

from crawlers.threads import crawl_threads, discover_threads
from crawlers.topics import is_on_topic_threads


class CandidateFromCardTests(unittest.TestCase):
    def test_missing_post_id_rejected(self):
        self.assertIsNone(discover_threads.candidate_from_card({"url": "https://www.threads.net/post/abc"}))

    def test_missing_url_rejected(self):
        self.assertIsNone(discover_threads.candidate_from_card({"post_id": "abc"}))

    def test_valid_card_normalized(self):
        card = {
            "post_id": "abc123",
            "url": "https://www.threads.net/post/abc123",
            "post_text": "Ngập lụt Hà Nội khiến giao thông tê liệt #ngaphanoi",
            "posted_at_epoch": 1756700000,
        }
        candidate = discover_threads.candidate_from_card(card)
        self.assertEqual(candidate["post_id"], "abc123")
        self.assertIn("#ngaphanoi", candidate["hashtags"])

    def test_hashtags_extracted_from_text_when_absent(self):
        card = {"post_id": "x", "url": "u", "post_text": "kẹt xe #tacduong ngập nước"}
        candidate = discover_threads.candidate_from_card(card)
        self.assertEqual(candidate["hashtags"], ["#tacduong"])


class DedupCandidatesTests(unittest.TestCase):
    def test_dedups_by_post_id(self):
        existing = {}
        first = [{"post_id": "a", "url": "u1"}]
        merged, added = discover_threads.dedup_candidates(existing, first)
        self.assertEqual(added, 1)
        merged, added = discover_threads.dedup_candidates(merged, [{"post_id": "a", "url": "u1-dup"}])
        self.assertEqual(added, 0)
        self.assertEqual(merged["a"]["url"], "u1")


class InDateWindowTests(unittest.TestCase):
    def test_before_since_excluded(self):
        self.assertFalse(discover_threads.in_date_window(100, since_epoch=200, until_epoch=None))

    def test_after_until_excluded(self):
        self.assertFalse(discover_threads.in_date_window(300, since_epoch=None, until_epoch=200))

    def test_within_window_included(self):
        self.assertTrue(discover_threads.in_date_window(150, since_epoch=100, until_epoch=200))


class KeepPrefillTests(unittest.TestCase):
    def test_accept_prefills_one(self):
        self.assertEqual(discover_threads.keep_prefill("accept"), "1")

    def test_reject_prefills_zero(self):
        self.assertEqual(discover_threads.keep_prefill("reject"), "0")


class GateWrapperTests(unittest.TestCase):
    def test_on_topic_post_accepted(self):
        verdict, _rule = is_on_topic_threads(
            "Ngập lụt Hà Nội khiến xe buýt tê liệt trên đường Nguyễn Trãi", []
        )
        self.assertEqual(verdict, "accept")

    def test_off_topic_post_rejected(self):
        verdict, rule = is_on_topic_threads("Hôm nay trời đẹp, đi chơi thôi", [])
        self.assertEqual(verdict, "reject")
        self.assertIn("missing_group", rule)

    def test_hashtags_contribute_to_gate(self):
        verdict, _rule = is_on_topic_threads(
            "Giao thông Hà Nội", ["#ngập", "#tắcđường"]
        )
        self.assertEqual(verdict, "accept")


class PostUrlHelpersTests(unittest.TestCase):
    def test_post_id_from_url(self):
        self.assertEqual(
            crawl_threads.post_id_from_url("https://www.threads.net/post/CxYz123?utm=abc"),
            "CxYz123",
        )

    def test_canonical_post_url_strips_query(self):
        self.assertEqual(
            crawl_threads.canonical_post_url("https://www.threads.net/post/CxYz123?utm=abc"),
            "https://www.threads.net/post/CxYz123",
        )


class NormalizeReplyCardTests(unittest.TestCase):
    def test_missing_reply_id_rejected(self):
        self.assertIsNone(crawl_threads.normalize_reply_card({"reply_text": "hi"}))

    def test_valid_reply_normalized(self):
        reply = crawl_threads.normalize_reply_card({
            "reply_id": "r1", "reply_text": "Đồng ý", "likes_count": "5", "author_handle": "someone",
        })
        self.assertEqual(reply["reply_id"], "r1")
        self.assertEqual(reply["likes_count"], 5)


class DedupPoolTests(unittest.TestCase):
    def test_dedups_by_reply_id(self):
        merged, added = crawl_threads.dedup_pool({}, [{"reply_id": "r1", "reply_text": "a"}])
        self.assertEqual(added, 1)
        merged, added = crawl_threads.dedup_pool(merged, [{"reply_id": "r1", "reply_text": "dup"}])
        self.assertEqual(added, 0)
        self.assertEqual(merged["r1"]["reply_text"], "a")


if __name__ == "__main__":
    unittest.main()
