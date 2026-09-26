import json
import os
import unittest

from crawlers.news.adapters import thanhnien, vietnamnet, vnexpress
from crawlers.news import news_common
from crawlers.news.crawl_news import _resolve_outlet

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_fixture(name, encoding="utf-8"):
    with open(os.path.join(FIXTURE_DIR, name), encoding=encoding) as f:
        return f.read()


class ExtractMetaTests(unittest.TestCase):
    def setUp(self):
        self.html = _load_fixture("vnexpress_article.html")

    def test_title_stripped_of_publisher_suffix(self):
        title, _body, _pub, _ids = vnexpress.extract_meta(self.html)
        self.assertNotIn("VnExpress", title)
        self.assertIn("khách", title)

    def test_body_extracted_for_gating(self):
        _title, body, _pub, _ids = vnexpress.extract_meta(self.html)
        self.assertGreater(len(body), 500)
        self.assertIn("xe buýt", body.lower())

    def test_published_at_is_iso_with_offset(self):
        _title, _body, published_at, _ids = vnexpress.extract_meta(self.html)
        self.assertRegex(published_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+07:00$")

    def test_comment_widget_ids_present(self):
        _title, _body, _pub, ids = vnexpress.extract_meta(self.html)
        self.assertEqual(set(ids), {"article_id", "article_type", "site_id", "category_id"})

    def test_missing_widget_returns_none_ids(self):
        _title, _body, _pub, ids = vnexpress.extract_meta("<title>x</title>")
        self.assertIsNone(ids)


class NormalizeCommentTests(unittest.TestCase):
    def test_strips_quoted_excerpt_prefix(self):
        item = {
            "comment_id": 63996244, "content": '"trích bài viết..."<br />Rất ủng hộ.',
            "creation_time": 1785050260, "userlike": 132, "full_name": "yousay2027", "is_pin": 0,
        }
        comment = vnexpress.normalize_comment(item)
        self.assertEqual(comment["content"], "Rất ủng hộ.")
        self.assertEqual(comment["native_comment_id"], 63996244)
        self.assertEqual(comment["likes_count"], 132)
        self.assertEqual(comment["author_name"], "yousay2027")
        self.assertFalse(comment["is_pinned"])

    def test_pinned_flag(self):
        comment = vnexpress.normalize_comment({"comment_id": 1, "content": "hi", "is_pin": 1})
        self.assertTrue(comment["is_pinned"])

    def test_reply_count_from_top_level_item(self):
        comment = vnexpress.normalize_comment({
            "comment_id": 1, "content": "hi", "replys": {"total": 19, "items": []},
        })
        self.assertEqual(comment["reply_count"], 19)

    def test_reply_count_defaults_zero_for_reply_items(self):
        # Reply items (from getreplay) carry no "replys" field at all.
        comment = vnexpress.normalize_comment({"comment_id": 2, "parent_id": 1, "content": "re"})
        self.assertEqual(comment["reply_count"], 0)


class ToRecordTests(unittest.TestCase):
    def test_record_has_required_fields_and_hashed_author(self):
        comment = vnexpress.normalize_comment({
            "comment_id": 1, "content": "Ngập nặng ảnh hưởng giao thông", "creation_time": 1785050260,
            "userlike": 5, "full_name": "real_name_123", "is_pin": 0,
        })
        record = news_common.to_record(
            article_url="https://vnexpress.net/a-123.html", publisher="VnExpress",
            title="Ngập lụt Hà Nội", published_at="2026-07-07T19:37:15+07:00",
            comment=comment, batch_id_value="news_test", handle_salt="salt",
            topic_rule="du_3_nhom", matched_groups_={"A_ngap_lut"},
            discovery_query="ngập Hà Nội", source_class="policy_search",
        )
        for key in ("id", "platform", "source_url", "post_context", "article_published_at",
                    "comment_text", "posted_at_raw", "likes_count", "author_hash", "matched_groups"):
            self.assertIn(key, record)
        self.assertNotIn("real_name_123", str(record))
        self.assertEqual(record["article_published_at"], "2026-07-07T19:37:15+07:00")
        self.assertFalse(record["is_reply"])
        self.assertIsNone(record["parent_native_comment_id"])

    def test_reply_record_is_flagged_and_linked_to_parent(self):
        reply = vnexpress.normalize_comment({"comment_id": 2, "parent_id": 1, "content": "re: hi"})
        record = news_common.to_record(
            article_url="https://vnexpress.net/a-123.html", publisher="VnExpress",
            title="Ngập lụt Hà Nội", published_at="", comment=reply,
            batch_id_value="news_test", handle_salt="salt", topic_rule="du_3_nhom",
            matched_groups_=set(), discovery_query="", source_class="policy_search",
            is_reply=True, parent_native_comment_id=1,
        )
        self.assertTrue(record["is_reply"])
        self.assertEqual(record["parent_native_comment_id"], 1)

    def test_same_comment_same_id_twice(self):
        comment = vnexpress.normalize_comment({"comment_id": 42, "content": "x"})
        kwargs = dict(article_url="https://vnexpress.net/a.html", publisher="VnExpress",
                      title="t", published_at="", comment=comment, batch_id_value="b",
                      handle_salt="s", topic_rule="", matched_groups_=set(),
                      discovery_query="", source_class="")
        self.assertEqual(news_common.to_record(**kwargs)["id"], news_common.to_record(**kwargs)["id"])


class ResolveOutletTests(unittest.TestCase):
    def test_vnexpress_url_resolves(self):
        outlet_key, outlet, adapter = _resolve_outlet("https://vnexpress.net/a-123456789.html")
        self.assertEqual(outlet_key, "vnexpress")
        self.assertIs(adapter, vnexpress)
        self.assertEqual(outlet["publisher_name"], "VnExpress")

    def test_thanhnien_url_resolves(self):
        outlet_key, outlet, adapter = _resolve_outlet(
            "https://thanhnien.vn/mot-bai-viet-185260701162022832.htm")
        self.assertEqual(outlet_key, "thanhnien")
        self.assertIs(adapter, thanhnien)
        self.assertEqual(outlet["publisher_name"], "Thanh Nien")

    def test_vietnamnet_url_resolves(self):
        outlet_key, outlet, adapter = _resolve_outlet(
            "https://vietnamnet.vn/mot-bai-viet-2540481.html")
        self.assertEqual(outlet_key, "vietnamnet")
        self.assertIs(adapter, vietnamnet)
        self.assertEqual(outlet["publisher_name"], "VietnamNet")

    def test_unrelated_url_does_not_resolve(self):
        outlet_key, outlet, adapter = _resolve_outlet("https://example.com/a.html")
        self.assertIsNone(outlet_key)
        self.assertIsNone(outlet)
        self.assertIsNone(adapter)


# --------------------------------------------------------------------------
# Thanh Nien adapter -- mechanism confirmed live 2026-08-09 (see
# adapters/thanhnien.py docstring). Unlike VnExpress this is an HTML
# fragment API, and replies are inlined in the same response rather than a
# separate endpoint.
# --------------------------------------------------------------------------


class ThanhNienExtractMetaTests(unittest.TestCase):
    def setUp(self):
        self.html = _load_fixture("thanhnien_article.html")

    def test_title_from_hidden_field(self):
        title, _body, _pub, _ids = thanhnien.extract_meta(self.html)
        self.assertIn("xe buýt", title.lower())

    def test_body_extracted_for_gating(self):
        _title, body, _pub, _ids = thanhnien.extract_meta(self.html)
        self.assertGreater(len(body), 500)

    def test_published_at_is_iso_with_offset(self):
        _title, _body, published_at, _ids = thanhnien.extract_meta(self.html)
        self.assertRegex(published_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+07:00$")

    def test_comment_widget_ids_present(self):
        _title, _body, _pub, ids = thanhnien.extract_meta(self.html)
        self.assertEqual(set(ids), {"news_id"})
        self.assertTrue(ids["news_id"].isdigit())

    def test_missing_widget_returns_none_ids(self):
        _title, _body, _pub, ids = thanhnien.extract_meta("<title>x</title>")
        self.assertIsNone(ids)


class ThanhNienFetchCommentsTests(unittest.TestCase):
    def setUp(self):
        self.fragment = _load_fixture("thanhnien_comment_page.html")
        self.outlet = news_common.OUTLETS["thanhnien"]

    def _fetch(self, fragments):
        calls = iter(fragments)

        def fake_fetch_text(_url):
            return next(calls, "")

        original = thanhnien.fetch_text
        thanhnien.fetch_text = fake_fetch_text
        try:
            ids = {"news_id": "185260712201942802"}
            comments = list(thanhnien.fetch_comments(self.outlet, ids, 20, 500))
            return comments, ids
        finally:
            thanhnien.fetch_text = original

    def test_parses_all_top_level_comments(self):
        comments, _ids = self._fetch([self.fragment])
        self.assertEqual(len(comments), 20)

    def test_author_names_are_unescaped(self):
        comments, _ids = self._fetch([self.fragment])
        self.assertTrue(any("Bạn đọc mới" == c["author_name"] for c in comments))

    def test_reply_count_and_stash_for_fetch_replies(self):
        comments, ids = self._fetch([self.fragment])
        with_reply = next(c for c in comments if c["reply_count"] > 0)
        self.assertEqual(with_reply["reply_count"], 1)
        replies = list(thanhnien.fetch_replies(
            self.outlet, ids, with_reply["native_comment_id"], 1, 20, 500))
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["author_name"], "truong hao")
        self.assertNotIn("@", replies[0]["content"][:1])

    def test_empty_first_page_yields_no_comments(self):
        comments, _ids = self._fetch([""])
        self.assertEqual(comments, [])


class ThanhNienDiscoverUrlsTests(unittest.TestCase):
    def test_extracts_and_filters_article_links(self):
        html = (
            '<a href="/bai-viet-mot-185260701162022832.htm">a</a>'
            '<a href="/chinh-tri.htm">not an article</a>'
            '<a href="/bai-viet-hai-185260701162022833.htm">b</a>'
        )
        original = thanhnien.fetch_text
        thanhnien.fetch_text = lambda _url: html
        try:
            urls = thanhnien.discover_urls(news_common.OUTLETS["thanhnien"], "xe buýt", 10)
        finally:
            thanhnien.fetch_text = original
        self.assertEqual(urls, [
            "https://thanhnien.vn/bai-viet-mot-185260701162022832.htm",
            "https://thanhnien.vn/bai-viet-hai-185260701162022833.htm",
        ])

    def test_respects_max_results(self):
        html = "".join(
            f'<a href="/bai-viet-{i}-18526070116200{i:02d}.htm">x</a>' for i in range(5)
        )
        original = thanhnien.fetch_text
        thanhnien.fetch_text = lambda _url: html
        try:
            urls = thanhnien.discover_urls(news_common.OUTLETS["thanhnien"], "xe buýt", 2)
        finally:
            thanhnien.fetch_text = original
        self.assertEqual(len(urls), 2)


# --------------------------------------------------------------------------
# VietnamNet adapter -- mechanism confirmed live 2026-08-09 (see
# adapters/vietnamnet.py docstring). Search is bot-protected so discovery
# uses RSS feeds + client-side keyword filtering instead; comments are a
# real JSON API and replies reuse the same endpoint via a ParentId param.
# --------------------------------------------------------------------------


class VietnamNetExtractMetaTests(unittest.TestCase):
    def setUp(self):
        self.html = _load_fixture("vietnamnet_article.html")

    def test_title_from_title_tag(self):
        title, _body, _pub, _ids = vietnamnet.extract_meta(self.html)
        self.assertIn("xe buýt", title.lower())

    def test_body_extracted_for_gating(self):
        _title, body, _pub, _ids = vietnamnet.extract_meta(self.html)
        self.assertGreater(len(body), 500)

    def test_published_at_is_normalized_iso_with_offset(self):
        # Raw JSON-LD value has millis and a space before the offset
        # ("...000 +07:00") -- extract_meta must strip both so the shape
        # matches VnExpress/Thanh Nien's clean "+07:00"-suffixed output.
        _title, _body, published_at, _ids = vietnamnet.extract_meta(self.html)
        self.assertRegex(published_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+07:00$")

    def test_comment_widget_ids_present(self):
        _title, _body, _pub, ids = vietnamnet.extract_meta(self.html)
        self.assertEqual(set(ids), {"object_id", "website_id"})
        self.assertTrue(ids["object_id"].isdigit())

    def test_missing_widget_returns_none_ids(self):
        _title, _body, _pub, ids = vietnamnet.extract_meta("<title>x</title>")
        self.assertIsNone(ids)


class VietnamNetFetchCommentsTests(unittest.TestCase):
    def setUp(self):
        self.comment_page = json.loads(_load_fixture("vietnamnet_comment_page.json"))
        self.reply_page = json.loads(_load_fixture("vietnamnet_reply_page.json"))
        self.outlet = news_common.OUTLETS["vietnamnet"]
        self.ids = {"object_id": "2540481", "website_id": "000003"}

    def _fetch_comments(self, pages):
        calls = iter(pages)

        def fake_fetch_json(_url, _params):
            return next(calls, {"status": True, "data": {"comments": []}})

        original = vietnamnet.fetch_json
        vietnamnet.fetch_json = fake_fetch_json
        try:
            return list(vietnamnet.fetch_comments(self.outlet, self.ids, 25, 500))
        finally:
            vietnamnet.fetch_json = original

    def test_parses_all_top_level_comments(self):
        comments = self._fetch_comments([self.comment_page])
        self.assertEqual(len(comments), 20)

    def test_no_duplicate_ids(self):
        comments = self._fetch_comments([self.comment_page])
        ids = [c["native_comment_id"] for c in comments]
        self.assertEqual(len(ids), len(set(ids)))

    def test_reply_count_from_total_answer(self):
        comments = self._fetch_comments([self.comment_page])
        with_reply = next(c for c in comments if c["reply_count"] > 0)
        self.assertEqual(with_reply["native_comment_id"], "4377189")
        self.assertEqual(with_reply["reply_count"], 1)

    def test_content_tags_stripped(self):
        comments = self._fetch_comments([self.comment_page])
        self.assertTrue(all("<p>" not in c["content"] for c in comments))

    def test_pagination_stops_on_empty_page_not_total_field(self):
        # Regression test: totalComment counts top-level + reply comments
        # combined (confirmed live 2026-08-09), so it must NOT be used as
        # the top-level pagination stop condition -- a second page request
        # returning empty is the only reliable signal.
        comments = self._fetch_comments([self.comment_page, {"status": True, "data": {"comments": []}}])
        self.assertEqual(len(comments), 20)

    def test_fetch_replies_via_same_endpoint(self):
        calls = []

        def fake_fetch_json(_url, params):
            calls.append(params)
            if params.get("ParentId") == "4377189" and len(calls) == 1:
                return self.reply_page
            return {"status": True, "data": {"comments": []}}

        original = vietnamnet.fetch_json
        vietnamnet.fetch_json = fake_fetch_json
        try:
            replies = list(vietnamnet.fetch_replies(self.outlet, self.ids, "4377189", 1, 25, 500))
        finally:
            vietnamnet.fetch_json = original
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["native_comment_id"], "4377216")

    def test_error_status_raises(self):
        original = vietnamnet.fetch_json
        vietnamnet.fetch_json = lambda _url, _params: {"status": False, "messages": ["boom"]}
        try:
            with self.assertRaises(news_common.NewsAPIError):
                list(vietnamnet.fetch_comments(self.outlet, self.ids, 25, 500))
        finally:
            vietnamnet.fetch_json = original


class VietnamNetDiscoverUrlsTests(unittest.TestCase):
    def setUp(self):
        self.rss_text = _load_fixture("vietnamnet_rss_feed.xml")

    def _discover(self, keyword, max_results=10):
        outlet = dict(news_common.OUTLETS["vietnamnet"])
        original = vietnamnet.fetch_text
        vietnamnet.fetch_text = lambda _url: self.rss_text
        try:
            return vietnamnet.discover_urls(outlet, keyword, max_results), outlet
        finally:
            vietnamnet.fetch_text = original

    def test_finds_relevant_items_by_title_match(self):
        urls, _outlet = self._discover("xe buýt")
        self.assertTrue(any("xe-buyt" in u for u in urls))

    def test_no_match_returns_empty(self):
        urls, _outlet = self._discover("khong co tu khoa nao khop ca")
        self.assertEqual(urls, [])

    def test_rss_items_cached_across_calls(self):
        outlet = dict(news_common.OUTLETS["vietnamnet"])
        calls = []

        def fake_fetch_text(url):
            calls.append(url)
            return self.rss_text

        original = vietnamnet.fetch_text
        vietnamnet.fetch_text = fake_fetch_text
        try:
            vietnamnet.discover_urls(outlet, "xe buýt", 10)
            first_call_count = len(calls)
            vietnamnet.discover_urls(outlet, "miễn phí", 10)
        finally:
            vietnamnet.fetch_text = original
        self.assertEqual(first_call_count, len(outlet["rss_categories"]))
        self.assertEqual(len(calls), first_call_count)  # no new fetches on second call


if __name__ == "__main__":
    unittest.main()
