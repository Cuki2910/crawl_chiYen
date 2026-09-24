import csv
import inspect
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from crawlers.facebook import crawl_facebook as fb
from crawlers.facebook import facebook_session as fbs
from crawlers.facebook import facebook_worker as worker


COMMENT_TIME = "Thứ bảy, 11 Tháng 7, 2026 lúc 17:43"
COMMENT_TIME_ISO = "2026-07-11T17:43:00+07:00"
SECOND_COMMENT_TIME = "Thứ Tư, 1 Tháng 7, 2026 lúc 12:57"

SOURCE = {
    "kind": "search",
    "label": "access_barriers_hcm",
    "source_class": "access_search",
    "query": "xe buýt TP.HCM CCCD VNeID điện thoại thẻ ngân hàng người lớn tuổi khuyết tật thu nhập thấp",
    "max_posts": 8,
}


class FacebookCrawlerTests(unittest.TestCase):
    def test_canonical_identity_matches_permalink_variants(self):
        a = "https://www.facebook.com/gtcctphcm/posts/pfbid02abc?comment_id=1"
        b = "https://m.facebook.com/gtcctphcm/posts/pfbid02abc/"
        other = "https://www.facebook.com/gtcctphcm/posts/pfbid02other"
        self.assertEqual(fb.canonical_post_identity(a), fb.canonical_post_identity(b))
        self.assertNotEqual(fb.canonical_post_identity(a), fb.canonical_post_identity(other))
        self.assertEqual(fb.canonical_post_url(a), "https://www.facebook.com/gtcctphcm/posts/pfbid02abc")
        self.assertTrue(fb.url_matches_target(b, a))
        self.assertFalse(fb.url_matches_target(other, a))

    def test_video_canonicalization_distinguishes_reel_and_watch_urls(self):
        reel = "https://m.facebook.com/reel/123456789/?comment_id=42"
        watch = "https://www.facebook.com/watch/?v=987654321&ref=sharing"
        video = "https://www.facebook.com/example/videos/555"
        self.assertEqual(fb.canonical_post_url(reel), "https://www.facebook.com/reel/123456789")
        self.assertEqual(fb.canonical_post_identity(reel), "123456789")
        self.assertEqual(fb.canonical_post_url(watch), "https://www.facebook.com/watch?v=987654321")
        self.assertEqual(fb.canonical_post_identity(watch), "987654321")
        self.assertEqual(fb.content_type_for_url(reel), "reel")
        self.assertEqual(fb.content_type_for_url(watch), "video")
        self.assertEqual(fb.content_type_for_url(video), "video")

    def test_reply_js_uses_permalink_ids_and_vietnamese_controls(self):
        for script in (fb._CAPTURE_COMMENTS_JS, fb._CAPTURE_REEL_COMMENTS_JS):
            self.assertIn("searchParams.get('reply_comment_id')", script)
            self.assertIn("searchParams.get('comment_id')", script)
            self.assertIn("const commentId = replyId || topId", script)
            self.assertIn("permalinkLink.getAttribute('aria-label')", script)
            self.assertIn(".filter(belongsToBlock)", script)
            self.assertIn("block.getAttribute('aria-label')", script)
        self.assertIn("B\\u00ecnh lu\\u1eadn d\\u01b0\\u1edbi t\\u00ean", fb._CAPTURE_COMMENTS_JS)
        post_expand_source = inspect.getsource(fb._scroll_and_expand_page)
        for script in (post_expand_source, fb._EXPAND_REEL_COMMENTS_JS):
            self.assertIn("câu trả lời", script)
            self.assertIn("button, span", script)
            self.assertIn("closest('[role=\"button\"], button')", script)

    def test_extract_creation_story_message_text_decodes_vietnamese(self):
        html = r'<script>{"creation_story":{"message":{"text":"Xe bu\u00fdt TP.HCM mi\u1ec5n ph\u00ed"}}}</script>'
        self.assertEqual(fb.extract_creation_story_message_text(html), "Xe bu\u00fdt TP.HCM mi\u1ec5n ph\u00ed")

    def test_reel_metadata_prefers_creation_story_body(self):
        captured = {"resolved": True, "body": "title fallback", "published_at_raw": "2026-07-01T10:30:00Z"}
        response = _FakeResponse(captured, r'{"creation_story":{"message":{"text":"Xe bu\u00fdt HCM mi\u1ec5n ph\u00ed"}}}')
        metadata = fb._metadata_from_response(response, "https://www.facebook.com/reel/123")
        self.assertEqual(metadata["post_context"], "Xe buýt HCM miễn phí")
        self.assertTrue(metadata["metadata_resolved"])
        self.assertEqual(metadata["post_published_at"], "2026-07-01T10:30:00+00:00")

    def test_post_metadata_keeps_title_and_captured_article_body(self):
        response = _FakeResponse({"resolved": True, "title": "Hà Nội ngập đường", "body": "Xe buýt bị chậm", "published_at_raw": "2 giờ"})
        metadata = fb._metadata_from_response(response, "https://www.facebook.com/example/posts/123")
        self.assertEqual(metadata["post_title"], "Hà Nội ngập đường")
        self.assertEqual(metadata["post_context"], "Xe buýt bị chậm")
        self.assertEqual(metadata["post_published_at_raw"], "2 giờ")
        self.assertEqual(metadata["post_published_at"], "")
        self.assertEqual(fb.gate_metadata(metadata), ("accept", "flood_transport_hanoi"))

    def test_absolute_post_time_is_extracted_from_tooltip_text(self):
        raw = "Đã chia sẻ: 25/07/2026 14:30"
        self.assertEqual(fb._parse_absolute_post_time(raw), "2026-07-25T14:30:00")
        response = _FakeResponse({
            "resolved": True, "title": "Xe buýt miễn phí", "body": "TP.HCM",
            "published_at_candidates": ["2 giờ", raw],
        })
        metadata = fb._metadata_from_response(response, "https://www.facebook.com/example/posts/123")
        self.assertEqual(metadata["post_published_at_raw"], raw)
        self.assertEqual(metadata["post_published_at"], "2026-07-25T14:30:00")
        self.assertIn("[role=\"tooltip\"]", fb._CAPTURE_POST_METADATA_JS)
        self.assertIn("data-tooltip-content", fb._CAPTURE_POST_METADATA_JS)

    def test_photo_metadata_uses_creation_story_body(self):
        response = _FakeResponse(
            {"resolved": False, "body": "", "published_at_raw": ""},
            r'{"creation_story":{"message":{"text":"Xe buýt miễn phí TP.HCM"}}}',
        )
        metadata = fb._metadata_from_response(response, "https://www.facebook.com/photo?fbid=123")
        self.assertTrue(metadata["metadata_resolved"])
        self.assertEqual(metadata["post_context"], "Xe buýt miễn phí TP.HCM")

    def test_reject_and_unresolved_metadata_never_fetch_comments(self):
        for metadata, reason in (
            ({"metadata_resolved": True, "post_title": "Hà Nội ngập", "post_context": "người dân", "post_published_at": "2025-08-02T00:00:00+07:00"}, "missing_group:transport_impact"),
            ({"metadata_resolved": False, "post_context": "", "post_published_at": "2025-08-02T00:00:00+07:00"}, "metadata_unresolved"),
        ):
            with self.subTest(reason=reason), patch.object(fb, "fetch_post_metadata", return_value=metadata), patch.object(fb, "fetch_post") as fetch_comments:
                _, verdict, got_reason, _, comments = fb.fetch_eligible_post("https://facebook.com/posts/1")
                self.assertEqual((verdict, got_reason, comments), ("reject", reason, []))
                fetch_comments.assert_not_called()

    def test_js_has_no_broad_capture_fallback(self):
        self.assertNotIn("div[role=\"main\"]", fb._INIT_COMMENT_CAPTURE_JS)
        self.assertIn("identity(href) === targetId", fb._INIT_COMMENT_CAPTURE_JS)
        self.assertNotIn("topLevel.length ? [topLevel[0]]", fb._INIT_COMMENT_CAPTURE_JS)
        self.assertIn("mappedIdentity === targetId", fb._INIT_COMMENT_CAPTURE_JS)
        self.assertIn("isPhotoTarget || bodyMatches", fb._INIT_COMMENT_CAPTURE_JS)
        self.assertIn("commentContextIds.length === 1", fb._INIT_COMMENT_CAPTURE_JS)
        self.assertIn("chưa có bình luận nào", fb._INIT_COMMENT_CAPTURE_JS)
        self.assertIn("div[role=\"article\"]", fb._INIT_COMMENT_CAPTURE_JS)
        self.assertIn("scope.querySelectorAll", fb._CAPTURE_REEL_COMMENTS_JS)
        self.assertNotIn("document.querySelectorAll('div[role=\"article\"]')", fb._CAPTURE_REEL_COMMENTS_JS)

    def test_is_on_topic_requires_all_three_groups(self):
        accepts = [
            "Hà Nội ngập đường, xe buýt chậm",
            "Hà Nội mưa lớn gây ngập, giao thông tê liệt",
            "Đường Hà Nội ngập sâu, người đi xe bus bị trễ",
        ]
        for text in accepts:
            ok, rule = fb.is_on_topic(text, SOURCE)
            self.assertTrue(ok, (text, rule))

        for text in ("Xe buýt Hà Nội chờ lâu", "Hà Nội ngập sâu", "miễn phí xe buýt"):
            ok, _ = fb.is_on_topic(text, SOURCE)
            self.assertFalse(ok, text)

    def test_is_on_topic_rejects_off_topic(self):
        rejects = [
            "tour du lịch đảo cuối tuần",
            "VNeID là ứng dụng định danh điện tử mới",
            "người lớn tuổi cần được hỗ trợ y tế",
            "sale vé ca nhạc giải trí",
        ]
        neutral = {"kind": "search", "label": "generic", "source_class": "generic", "query": "VNeID", "max_posts": 3}
        for text in rejects:
            ok, _ = fb.is_on_topic(text, neutral)
            self.assertFalse(ok, text)

    def test_comment_tooltip_parser_is_strict_and_uses_vietnam_time(self):
        self.assertEqual(fb.parse_comment_tooltip(COMMENT_TIME), COMMENT_TIME_ISO)
        self.assertEqual(
            fb.parse_comment_tooltip("Thứ bảy, 11 Tháng 7, 2026 lúc 17:43"),
            COMMENT_TIME_ISO,
        )
        self.assertEqual(
            fb.parse_comment_tooltip("Saturday, July 11, 2026 at 5:43 PM"),
            COMMENT_TIME_ISO,
        )
        for raw in ("", "2 tuần trước", "Bình luận dưới tên A vào 2 tuần trước",
                    "Thứ bảy, 31 Tháng 2, 2026 lúc 17:43", f"{COMMENT_TIME} Đã chỉnh sửa"):
            with self.subTest(raw=raw):
                self.assertEqual(fb.parse_comment_tooltip(raw), "")

    def test_fetch_post_filters_invalid_timestamps_before_cap(self):
        raw_comments = [
            {"comment_text": "invalid", "posted_at_raw": "2 tuần trước"},
            {"comment_text": "valid one", "posted_at_raw": COMMENT_TIME},
            {"comment_text": "valid two", "posted_at_raw": SECOND_COMMENT_TIME},
        ]
        response = _FakeCommentResponse(raw_comments)
        with patch.object(fb, "_fetch", return_value=response):
            _, comments = fb.fetch_post(
                "https://www.facebook.com/watch?v=998112859703879",
                max_comments=1,
                metadata={"post_context": "ctx"},
            )
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["comment_text"], "valid one")
        self.assertEqual(comments[0]["posted_at_raw"], COMMENT_TIME)
        self.assertEqual(comments[0]["posted_at"], COMMENT_TIME_ISO)

    def test_to_record_rejects_missing_or_relative_comment_time(self):
        for raw in ("", "2 tuần trước"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                fb.to_record("https://www.facebook.com/posts/1", "ctx", {
                    "comment_text": "comment", "posted_at_raw": raw,
                }, "batch")

    def test_to_record_keeps_legacy_fields_adds_provenance_and_full_text_ids(self):
        first = {"comment_text": "a" * 50 + " one", "posted_at_raw": COMMENT_TIME, "likes_count": 2}
        second = {"comment_text": "a" * 50 + " two", "posted_at_raw": COMMENT_TIME, "likes_count": 2}
        r1 = fb.to_record("https://www.facebook.com/gtcctphcm/posts/pfbid02abc?x=1", "ctx", first, "batch", SOURCE, "flood_transport_hanoi")
        r2 = fb.to_record("https://www.facebook.com/gtcctphcm/posts/pfbid02abc", "ctx", second, "batch", SOURCE, "flood_transport_hanoi")

        for key in ("id", "platform", "source_url", "post_context", "comment_text", "posted_at_raw", "posted_at", "likes_count", "crawled_at", "crawl_batch_id"):
            self.assertIn(key, r1)
        self.assertEqual(r1["posted_at_raw"], COMMENT_TIME)
        self.assertEqual(r1["posted_at"], COMMENT_TIME_ISO)
        self.assertEqual(r1["capture_scope"], "target_permalink_article")
        self.assertEqual(r1["content_type"], "post")
        self.assertEqual(r1["source_kind"], "search")
        self.assertEqual(r1["source_class"], "access_search")
        self.assertEqual(r1["source_label"], "access_barriers_hcm")
        self.assertEqual(r1["discovery_query"], SOURCE["query"])
        self.assertEqual(r1["topic_rule"], "flood_transport_hanoi")
        self.assertNotEqual(r1["id"], r2["id"])

    def test_to_record_marks_reel_metadata(self):
        source = {"kind": "public_page", "label": "future_reel_page", "source_class": "official_page", "url": "https://www.facebook.com/example/reels/", "max_posts": 8}
        comment = {"comment_text": "Xe buyt rat tien", "posted_at_raw": COMMENT_TIME, "likes_count": 1}
        record = fb.to_record("https://www.facebook.com/reel/123456789?x=1", "ctx", comment, "batch", source, "transport_service_hcm")
        self.assertEqual(record["content_type"], "reel")
        self.assertEqual(record["capture_scope"], "target_reel_comments")
        self.assertEqual(record["source_kind"], "public_page")
        self.assertEqual(record["source_url"], "https://www.facebook.com/reel/123456789")

    def test_to_record_marks_watch_video_metadata(self):
        source = {"kind": "video_search", "label": "reels_bus_hcm", "source_class": "service_search", "query": "xe buyt TP.HCM", "max_posts": 8}
        comment = {"comment_text": "Xe buyt rat tien", "posted_at_raw": COMMENT_TIME, "likes_count": 1}
        record = fb.to_record("https://www.facebook.com/watch/?v=987654321", "ctx", comment, "batch", source, "transport_service_hcm")
        self.assertEqual(record["content_type"], "video")
        self.assertEqual(record["capture_scope"], "target_video_comments")
        self.assertEqual(record["source_kind"], "video_search")
        self.assertEqual(record["source_url"], "https://www.facebook.com/watch?v=987654321")

    def test_to_record_same_text_different_posted_at_raw_ids_differ(self):
        same_text = {"comment_text": "same comment text", "posted_at_raw": COMMENT_TIME, "likes_count": 0}
        same_text_later = {"comment_text": "same comment text", "posted_at_raw": SECOND_COMMENT_TIME, "likes_count": 0}
        url = "https://www.facebook.com/gtcctphcm/posts/pfbid02abc"
        r1 = fb.to_record(url, "ctx", same_text, "batch", SOURCE, "transport_service_hcm")
        r2 = fb.to_record(url, "ctx", same_text_later, "batch", SOURCE, "transport_service_hcm")
        self.assertNotEqual(r1["id"], r2["id"])

    def test_fetch_reuses_injected_browser_session(self):
        session = MagicMock()
        session.fetch.side_effect = [
            _FakeAuthResponse("ok"),
            _FakeAuthResponse("ok"),
        ]
        fb.use_browser_session(session)
        try:
            fb._fetch("https://www.facebook.com/a")
            fb._fetch("https://www.facebook.com/b")
        finally:
            fb.use_browser_session(None)
        self.assertEqual(session.fetch.call_count, 2)

    def test_fetch_raises_precise_midcrawl_auth_loss(self):
        session = MagicMock()
        session.fetch.return_value = _FakeAuthResponse("login_form")
        fb.use_browser_session(session)
        try:
            with self.assertRaises(fbs.FacebookAuthRequiredError) as raised:
                fb._fetch("https://www.facebook.com/search/posts")
        finally:
            fb.use_browser_session(None)
        self.assertEqual(raised.exception.state, "login_form")

    def test_fetch_rejects_missing_action_sentinel(self):
        session = MagicMock()
        session.fetch.return_value = _FakeResponse({}, "")
        fb.use_browser_session(session)
        try:
            with self.assertRaises(fb.FacebookCaptureError):
                fb._fetch("https://www.facebook.com/posts/1")
        finally:
            fb.use_browser_session(None)

    def test_existing_legacy_output_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "facebook_comments.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=["id", "posted_at_raw"])
                writer.writeheader()
                writer.writerow({"id": "legacy", "posted_at_raw": "2 tuần trước"})
            with self.assertRaisesRegex(ValueError, "incompatible schema"):
                fb._load_existing_ids(path)

    def test_direct_post_discovery_is_canonical(self):
        source = {"kind": "direct_post", "url": "https://m.facebook.com/example/posts/123/?comment_id=4"}
        self.assertEqual(fb.discover_posts(source), ["https://www.facebook.com/example/posts/123"])

    def test_photo_fbid_is_stable_identity(self):
        url = "https://www.facebook.com/photo/?fbid=123&set=pcb.456"
        self.assertEqual(fb.canonical_post_url(url), "https://www.facebook.com/photo?fbid=123")
        self.assertEqual(fb.canonical_post_identity(url), "123")

    def test_comment_cap_stops_before_expansion(self):
        page = MagicMock()
        page.evaluate.side_effect = [None, None, 10, None, None]
        action = fb._scroll_and_expand_page("https://www.facebook.com/example/posts/123", 45, 10)
        action(page)
        page.mouse.wheel.assert_not_called()

    def test_session_profile_busy_is_classified(self):
        engine = MagicMock()
        engine.start.side_effect = RuntimeError("Failed to create a ProcessSingleton; profile is already in use")
        with patch.object(fbs.os, "makedirs"):
            browser = fbs.FacebookSession(session_factory=lambda **kwargs: engine)
            with self.assertRaises(fbs.FacebookProfileBusyError):
                browser.start()

    def test_auth_challenge_keeps_browser_open(self):
        engine = MagicMock()
        page = MagicMock()
        page.is_closed.return_value = False
        page.url = "https://www.facebook.com/checkpoint/"
        page.evaluate.return_value = fbs.AUTH_CHALLENGE
        engine.context.new_page.return_value = page
        browser = fbs.FacebookSession(session_factory=lambda **kwargs: engine)
        with patch.object(fbs, "load_credentials", return_value=("email", "password")):
            browser.start()
            state, detail = browser.ensure_login()
        self.assertEqual(state, fbs.AUTH_CHALLENGE)
        self.assertEqual(detail["challenge_shot"], fbs.CHALLENGE_SHOT)
        engine.close.assert_not_called()
        browser.close()
        engine.close.assert_called_once()

    def test_worker_auth_loss_requeues_task(self):
        store = MagicMock()
        task = {"id": 1, "payload": SOURCE}
        store.claim.side_effect = [task]
        store.kv_get.side_effect = lambda key, default=None: default
        browser = MagicMock()
        browser.ensure_login.return_value = (fbs.AUTH_OK, {})
        with patch.object(worker, "_session", return_value=browser), \
             patch.object(worker.fb, "discover_posts", side_effect=fbs.FacebookAuthRequiredError("challenge")):
            result = worker.run_once(store, {"run_id": "run"}, lambda: False, "batch")
        self.assertEqual(result["status"], "auth_required")
        store.defer_task.assert_called_once_with(1, delay=0)

    def test_worker_target_unresolved_continues_to_next_post(self):
        store = MagicMock()
        task = {"id": 1, "payload": SOURCE}
        store.claim.side_effect = [task, None]
        # Đã "revisit" sẵn trong pass này -> None thứ 2 trả thẳng queue_empty, không gọi
        # thêm _revisit_sources() (thứ không phải trọng tâm test này).
        store.kv_get.side_effect = lambda key, default=None: True if key == "facebook_revisited_this_pass" else default
        browser = MagicMock()
        browser.ensure_login.return_value = (fbs.AUTH_OK, {})
        good = ({"post_context": "", "post_published_at_raw": "", "post_published_at": "", "metadata_resolved": 1},
                "reject", "thieu_nhom:B_chinh_sach", "", [])
        with patch.object(worker, "_session", return_value=browser), \
             patch.object(worker.fb, "discover_posts", return_value=["https://facebook.com/posts/bad", "https://facebook.com/posts/good"]), \
             patch.object(worker.fb, "fetch_eligible_post", side_effect=[fb.FacebookTargetUnresolvedError("target_unresolved"), good]) as fetch:
            result = worker.run_once(store, {"run_id": "run"}, lambda: False, "batch")
        self.assertEqual(result["status"], "queue_empty")
        self.assertEqual(fetch.call_count, 2)
        store.defer_task.assert_not_called()
        store.complete_task.assert_called_once_with(1)

    def test_revisit_sources_reseeds_queue_after_hourly_stamp_changes(self):
        import shutil
        import tempfile
        from crawlers.baseline_store import BaselineStore

        output_dir = tempfile.mkdtemp(prefix="fb_revisit_")
        store = BaselineStore(base_dir=output_dir)
        try:
            worker.seed_queue(store)
            for row in store.conn.execute("SELECT id FROM discovery_queue WHERE platform='facebook'").fetchall():
                store.complete_task(row["id"])
            self.assertEqual(store.pending_count("facebook"), 0)
            worker._revisit_sources(store)
            self.assertGreater(store.pending_count("facebook"), 0)
            queued_before = store.pending_count("facebook")
            worker._revisit_sources(store)  # cùng giờ -> dedup, không tăng thêm.
            self.assertEqual(store.pending_count("facebook"), queued_before)
        finally:
            store.close()
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_worker_generic_capture_error_defers_source(self):
        store = MagicMock()
        task = {"id": 1, "payload": SOURCE}
        store.claim.return_value = task
        store.kv_get.side_effect = lambda key, default=None: default
        browser = MagicMock()
        browser.ensure_login.return_value = (fbs.AUTH_OK, {})
        with patch.object(worker, "_session", return_value=browser), \
             patch.object(worker.fb, "discover_posts", return_value=["https://facebook.com/posts/bad"]), \
             patch.object(worker.fb, "fetch_eligible_post", side_effect=fb.FacebookCaptureError("error:TimeoutError")):
            result = worker.run_once(store, {"run_id": "run"}, lambda: False, "batch")
        self.assertEqual(result, {"status": "retry_wait", "retry_seconds": 120})
        store.defer_task.assert_called_once_with(1, delay=120)
        store.complete_task.assert_not_called()

    def test_smoke_rejects_production_directory(self):
        from crawlers.facebook import facebook_smoke

        with self.assertRaises(ValueError):
            facebook_smoke.run_smoke(facebook_smoke.PRODUCTION_DIR)
        nested = os.path.join(facebook_smoke.PRODUCTION_DIR, "smoke")
        with self.assertRaises(ValueError):
            facebook_smoke.run_smoke(nested)


class _FakeNode:
    def __init__(self, attrib):
        self.attrib = attrib


class _FakeResponse:
    def __init__(self, captured, html_content=""):
        self._html = [_FakeNode({fb._METADATA_DATA_ATTR: json.dumps(captured, ensure_ascii=False)})]
        self.html_content = html_content

    def css(self, selector):
        return self._html if selector == "html" else []


class _FakeCommentResponse:
    def __init__(self, comments):
        self._html = [_FakeNode({
            fb._TARGET_RESOLVED_ATTR: "1",
            fb._COMMENT_DATA_ATTR: json.dumps(comments, ensure_ascii=False),
        })]
        self.html_content = ""

    def css(self, selector):
        return self._html if selector == "html" else []


class _FakeAuthResponse:
    def __init__(self, state):
        self._html = [_FakeNode({
            "data-auth-state": state,
            fb._ACTION_STATE_ATTR: "ok" if state == "ok" else "auth",
        })]

    def css(self, selector):
        return self._html if selector == "html" else []


if __name__ == "__main__":
    unittest.main()
