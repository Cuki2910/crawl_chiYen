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

    def test_reply_count_defaults_to_zero(self):
        reply = crawl_threads.normalize_reply_card({"reply_id": "r1", "reply_text": "hi"})
        self.assertEqual(reply["reply_count"], 0)

    def test_reply_count_parsed(self):
        reply = crawl_threads.normalize_reply_card({"reply_id": "r1", "reply_text": "hi", "reply_count": "3"})
        self.assertEqual(reply["reply_count"], 3)


class ToRecordNestingTests(unittest.TestCase):
    def _reply(self, reply_id="r1", reply_count=0):
        return {
            "reply_id": reply_id, "reply_text": "noi dung", "posted_at_epoch": 0,
            "likes_count": 0, "author_handle": "someone", "reply_count": reply_count,
        }

    def test_depth_one_reply_parents_to_post(self):
        record = crawl_threads.to_record(
            post_url="https://www.threads.net/post/p1", post_id="p1", post_text="post text",
            reply=self._reply(), batch_id_value="b1", handle_salt="salt",
            parent_id="p1", depth=1,
        )
        self.assertTrue(record["is_reply"])
        self.assertEqual(record["parent_id"], "p1")
        self.assertEqual(record["depth"], 1)

    def test_nested_reply_parents_to_parent_record_id(self):
        parent_record = crawl_threads.to_record(
            post_url="https://www.threads.net/post/p1", post_id="p1", post_text="post text",
            reply=self._reply(reply_id="r1", reply_count=2), batch_id_value="b1", handle_salt="salt",
            parent_id="p1", depth=1,
        )
        nested_record = crawl_threads.to_record(
            post_url="https://www.threads.net/post/p1", post_id="p1", post_text="post text",
            reply=self._reply(reply_id="r2"), batch_id_value="b1", handle_salt="salt",
            parent_id=parent_record["id"], depth=2,
        )
        self.assertEqual(nested_record["parent_id"], parent_record["id"])
        self.assertEqual(nested_record["depth"], 2)
        self.assertNotEqual(nested_record["id"], parent_record["id"])

    def test_reply_count_carried_onto_record(self):
        record = crawl_threads.to_record(
            post_url="https://www.threads.net/post/p1", post_id="p1", post_text="post text",
            reply=self._reply(reply_count=5), batch_id_value="b1", handle_salt="salt",
            parent_id="p1", depth=1,
        )
        self.assertEqual(record["reply_count"], 5)


class DedupPoolTests(unittest.TestCase):
    def test_dedups_by_reply_id(self):
        merged, added = crawl_threads.dedup_pool({}, [{"reply_id": "r1", "reply_text": "a"}])
        self.assertEqual(added, 1)
        merged, added = crawl_threads.dedup_pool(merged, [{"reply_id": "r1", "reply_text": "dup"}])
        self.assertEqual(added, 0)
        self.assertEqual(merged["r1"]["reply_text"], "a")


class RecordsFromPoolInlineChainTests(unittest.TestCase):
    """Real-world fixture: ptea4k's comment (reply_count=1) -> ich.nek's
    inline reply (reply_count=1) -> ptea4k's inline reply (reply_count=0).
    Confirmed live that a reply_count==1 reply's single child is always
    the very next card in document order -- no navigation needed. ``page``
    is never touched for this path since no card here has reply_count>1,
    so None is safe to pass."""

    def _pool(self):
        return [
            {"reply_id": "r1", "reply_text": "ptea4k top", "posted_at_epoch": 0,
             "likes_count": 0, "author_handle": "ptea4k", "reply_count": 1},
            {"reply_id": "r2", "reply_text": "ich.nek inline child", "posted_at_epoch": 0,
             "likes_count": 0, "author_handle": "ich.nek", "reply_count": 1},
            {"reply_id": "r3", "reply_text": "ptea4k inline grandchild", "posted_at_epoch": 0,
             "likes_count": 0, "author_handle": "ptea4k", "reply_count": 0},
        ]

    def _run(self, max_depth=3):
        return crawl_threads._records_from_pool(
            self._pool(), page=None, post_id="p1", canonical_url="https://www.threads.com/post/p1",
            post_text="post text", hashtags=[], topic_rule="rule", posted_at_iso="",
            batch_id_value="b1", handle_salt="salt", source={}, start_parent_id="p1",
            start_depth=1, max_depth=max_depth, max_rounds=5, stale_rounds=2, visited={"p1"},
        )

    def test_three_records_produced(self):
        self.assertEqual(len(self._run()), 3)

    def test_depths_increment_down_the_chain(self):
        records = self._run()
        self.assertEqual([r["depth"] for r in records], [1, 2, 3])

    def test_top_level_parents_to_post(self):
        records = self._run()
        self.assertEqual(records[0]["parent_id"], "p1")

    def test_each_inline_child_parents_to_the_previous_record(self):
        records = self._run()
        self.assertEqual(records[1]["parent_id"], records[0]["id"])
        self.assertEqual(records[2]["parent_id"], records[1]["id"])

    def test_chain_stops_extending_past_max_depth(self):
        # max_depth=2: ich.nek (depth 2) is kept, but the chain doesn't
        # extend to a depth-3 record for r3 -- no data is lost (r3 is
        # still emitted), but it falls back to being misattributed as a
        # false top-level sibling (known limitation, documented in
        # _records_from_pool's docstring: once the depth budget is
        # exhausted mid-chain, there's no way to tell a remaining inline
        # child apart from a genuine top-level reply).
        records = self._run(max_depth=2)
        self.assertEqual(len(records), 3)
        self.assertEqual([r["depth"] for r in records], [1, 2, 1])
        self.assertEqual(records[2]["parent_id"], "p1")

    def test_unrelated_sibling_after_zero_count_reply_stays_top_level(self):
        pool = self._pool() + [{
            "reply_id": "r4", "reply_text": "unrelated top-level reply", "posted_at_epoch": 0,
            "likes_count": 0, "author_handle": "someoneelse", "reply_count": 0,
        }]
        records = crawl_threads._records_from_pool(
            pool, page=None, post_id="p1", canonical_url="https://www.threads.com/post/p1",
            post_text="post text", hashtags=[], topic_rule="rule", posted_at_iso="",
            batch_id_value="b1", handle_salt="salt", source={}, start_parent_id="p1",
            start_depth=1, max_depth=5, max_rounds=5, stale_rounds=2, visited={"p1"},
        )
        self.assertEqual(records[3]["parent_id"], "p1")
        self.assertEqual(records[3]["depth"], 1)


class VisitedGuardTests(unittest.TestCase):
    """The cycle guard that stops the infinite-navigation bug: a reply_id
    already in ``visited`` is skipped entirely (no record, no navigation
    attempt), confirmed live necessary because navigating into a reply's
    own page can yield that same reply back as one of its own "replies"."""

    def test_already_visited_reply_produces_no_record(self):
        pool = [{"reply_id": "r1", "reply_text": "self-referencing", "posted_at_epoch": 0,
                  "likes_count": 0, "author_handle": "someone", "reply_count": 2}]
        records = crawl_threads._records_from_pool(
            pool, page=None, post_id="p1", canonical_url="https://www.threads.com/post/p1",
            post_text="post text", hashtags=[], topic_rule="rule", posted_at_iso="",
            batch_id_value="b1", handle_salt="salt", source={}, start_parent_id="p1",
            start_depth=1, max_depth=5, max_rounds=5, stale_rounds=2, visited={"p1", "r1"},
        )
        self.assertEqual(records, [])

    def test_processing_a_reply_adds_it_to_visited(self):
        pool = [{"reply_id": "r1", "reply_text": "hi", "posted_at_epoch": 0,
                  "likes_count": 0, "author_handle": "someone", "reply_count": 0}]
        visited = {"p1"}
        crawl_threads._records_from_pool(
            pool, page=None, post_id="p1", canonical_url="https://www.threads.com/post/p1",
            post_text="post text", hashtags=[], topic_rule="rule", posted_at_iso="",
            batch_id_value="b1", handle_salt="salt", source={}, start_parent_id="p1",
            start_depth=1, max_depth=5, max_rounds=5, stale_rounds=2, visited=visited,
        )
        self.assertIn("r1", visited)


if __name__ == "__main__":
    unittest.main()
