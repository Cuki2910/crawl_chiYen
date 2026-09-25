"""Facebook worker cho baseline_gate_v2.

Persistent profile (headful). Discovery round-robin nguồn seed + query tổ hợp
A+B+C. Metadata → gate → comments/replies. Contamination guard: comment phải
thuộc đúng bài đích. CAPTCHA/checkpoint → auth_required, pause worker Facebook.
"""
import os
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from crawlers.common.records import require_env_salt, salted_hash
from crawlers.facebook import crawl_facebook as fb
from crawlers.facebook import facebook_session as fbs
from crawlers.topics import co_thanh_pho_khac, matched_groups

_SESSIONS = threading.local()
_MAX_COMMENT_RECONCILIATION_RETRIES = 3


def _session():
    browser = getattr(_SESSIONS, "browser", None)
    if browser is None:
        browser = fbs.FacebookSession().start()
        _SESSIONS.browser = browser
    return browser


def close_session():
    browser = getattr(_SESSIONS, "browser", None)
    if browser is not None:
        browser.close()
        del _SESSIONS.browser
    fb.use_browser_session(None)


def _auth_required(store, task=None, state=fbs.AUTH_LOGIN_FORM):
    if task is not None:
        store.defer_task(task["id"], delay=0)
    store.kv_set("facebook_status", "auth_required")
    store.log_event("auth_required", "facebook", {"state": state})
    return {"status": "auth_required", "state": state}


def seed_queue(store):
    if not store.kv_get("facebook_seeded"):
        priority = 10
        for source in fb.SOURCES:
            store.enqueue("facebook", "discover", source, priority=priority)
        store.kv_set("facebook_seeded", True)
    if store.kv_get("facebook_comment_reconciliation_seeded"):
        return
    for context in store.facebook_contexts_needing_reconciliation():
        retry_count = int(context.get("comment_retry_count") or 0) + 1
        _queue_reconciliation_retry(store, context["source_url"], context["context_id"], retry_count)
    store.kv_set("facebook_comment_reconciliation_seeded", True)


def _revisit_sources(store):
    """Nạp lại toàn bộ SOURCES + combo mỗi giờ để bắt bài mới đăng sau khi seed ban đầu.

    seed_queue() chỉ chạy 1 lần (flag facebook_seeded); không có vòng lặp này, worker
    dừng hẳn ở queue_empty sau khi xử lý hết danh sách cố định 1 lần, kể cả khi
    trang/nguồn có bài mới. dedup_key theo giờ tránh spam khi queue rỗng bị check lại
    liên tục trong cùng giờ. is_reconciliation-style seen-set vẫn lọc bài đã xử lý.
    """
    stamp = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%Y%m%d%H")
    for source in fb.SOURCES:
        store.enqueue("facebook", "discover", source, priority=50,
                      dedup_key=f"facebook:revisit:{source['label']}:{stamp}")


def _contamination_ok(post_url, comments):
    """Reject nếu có dấu hiệu comment lẫn từ bài khác (fail-closed, threshold 0)."""
    # Capture đã giới hạn trong target article scope; đây là guard thứ hai:
    # comment rỗng đã bị store bỏ; kiểm tra không có comment nào quá dài bất thường
    # (thường là toàn bộ post body của bài khác lọt vào).
    for c in comments:
        if not c.get("post_url") or not fb.url_matches_target(c["post_url"], post_url):
            return False
        if len(c.get("comment_text", "")) > 8000:
            return False
    return True


def _to_records(store, post_url, post_context, metadata, comments, run_id, batch_id):
    handle_salt = require_env_salt("FB_HANDLE_SALT")
    records = []
    for c in comments:
        text = c.get("comment_text", "")
        posted_at_raw = c.get("posted_at_raw", "")
        posted_at = fb.parse_comment_tooltip(posted_at_raw)
        if not posted_at:
            continue
        canonical = fb.canonical_post_url(post_url)
        import hashlib
        raw_key = f"{canonical}|{fb._norm_text(text)}|{fb._norm_text(c.get('posted_at_raw',''))}|{c.get('parent_comment_id','')}"
        cid = c.get("comment_id") or hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:20]
        comment_type = c.get("comment_type", "top_level")
        parent_id = c.get("parent_comment_id") or None
        author_raw = c.get("author_raw", "")
        author_key = salted_hash(author_raw, handle_salt) if author_raw else None
        records.append({
            "platform": "facebook", "comment_id": cid, "context_id": fb.canonical_post_identity(post_url),
            "id": f"facebook_c_{cid}", "source_url": canonical, "post_context": post_context,
            "post_title": metadata.get("post_title", ""), "post_published_at_raw": metadata.get("post_published_at_raw", ""),
            "post_published_at": metadata.get("post_published_at", ""),
            "comment_text": text, "comment_type": comment_type, "parent_comment_id": parent_id,
            "thread_id": parent_id or cid, "reply_depth": 1 if comment_type == "reply" else 0,
            "posted_at_raw": posted_at_raw, "posted_at": posted_at,
            "likes_count": c.get("likes_count", 0), "author_key": author_key,
            "matched_groups": ",".join(matched_groups(text)),
            "co_thanh_pho_khac": int(co_thanh_pho_khac("", post_context)),
            "parent_unresolved": int(bool(c.get("parent_unresolved"))),
            "crawl_batch_id": batch_id, "run_id": run_id,
            "crawled_at": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat(),
        })
    return records


def _comment_reconciliation(capture, captured_count, max_comments):
    """Classify one capture without claiming completeness when the UI count is absent."""
    expected = capture.get("expected_total") if isinstance(capture, dict) else None
    if not isinstance(expected, int) or expected < 0:
        return None, None, "unverified"
    missing = max(expected - captured_count, 0)
    if max_comments is not None and expected > captured_count:
        return expected, missing, "limited"
    return expected, missing, "complete" if missing == 0 else "incomplete"


def _queue_reconciliation_retry(store, post_url, identity, retry_count):
    payload = {
        "kind": "direct_post",
        "label": "comment_reconciliation",
        "url": fb.canonical_post_url(post_url),
        "reconciliation_attempt": retry_count,
    }
    store.enqueue(
        "facebook", "reconcile", payload, priority=5,
        dedup_key=f"facebook:reconcile:{identity}:{retry_count}",
    )


def run_once(store, run, deadline_check, batch_id, max_comments=None, allow_revisit=True):
    """Xử lý task discover cho tới khi hết queue/giờ hoặc gặp auth challenge."""
    try:
        browser = _session()
    except fbs.FacebookProfileBusyError:
        store.kv_set("facebook_status", "profile_busy")
        store.log_event("profile_busy", "facebook", {"action": "close_other_facebook_profile_window"})
        return {"status": "auth_required", "state": "profile_busy"}
    fb.use_browser_session(browser)
    state, detail = browser.ensure_login()
    if state != fbs.AUTH_OK:
        store.kv_set("facebook_status", "auth_required")
        store.log_event("auth_required", "facebook", {"state": state, **detail})
        return {"status": "auth_required", "detail": detail, "state": state}
    store.kv_set("facebook_status", "running")
    seed_queue(store)
    store.kv_set("facebook_revisited_this_pass", not allow_revisit)
    seen = set(store.kv_get("facebook_seen_posts", []))

    while True:
        if deadline_check():
            store.kv_set("facebook_status", "deadline")
            return {"status": "deadline"}
        task = store.claim("facebook")
        if task is None:
            if allow_revisit and not store.kv_get("facebook_revisited_this_pass"):
                _revisit_sources(store)
                store.kv_set("facebook_revisited_this_pass", True)
                continue
            store.kv_set("facebook_status", "queue_empty")
            return {"status": "queue_empty"}
        store.kv_set("facebook_revisited_this_pass", False)

        source = task["payload"]
        try:
            post_urls = fb.discover_posts(source)
        except fbs.FacebookAuthRequiredError as exc:
            return _auth_required(store, task, exc.state)
        except fb.CookieExpiredError:
            return _auth_required(store, task)
        except Exception as exc:
            store.log_event("fb_discover_error", "facebook", {"source": source.get("label"), "error": repr(exc)[:200]})
            store.retry_task(task["id"], delay=120)
            continue

        for post_url in post_urls:
            if deadline_check():
                store.complete_task(task["id"])
                return {"status": "deadline"}
            identity = fb.canonical_post_identity(post_url)
            is_reconciliation = source.get("kind") == "direct_post" and source.get("label") == "comment_reconciliation"
            if identity in seen and not is_reconciliation:
                continue
            try:
                metadata, verdict, reason, post_context, comments = fb.fetch_eligible_post(post_url, max_comments=max_comments)
            except fbs.FacebookAuthRequiredError as exc:
                return _auth_required(store, task, exc.state)
            except fb.FacebookTargetUnresolvedError as exc:
                store.log_event("fb_capture_error", "facebook", {"url": post_url, "error": str(exc)[:200]})
                continue
            except fb.FacebookCaptureError as exc:
                store.defer_task(task["id"], delay=120)
                store.log_event("fb_capture_error", "facebook", {"url": post_url, "error": str(exc)[:200]})
                return {"status": "retry_wait", "retry_seconds": 120}
            except Exception as exc:
                store.log_event("fb_fetch_error", "facebook", {"url": post_url, "error": repr(exc)[:200]})
                continue
            seen.add(identity)
            context = {
                "platform": "facebook", "context_id": identity, "source_url": fb.canonical_post_url(post_url),
                "post_title": metadata.get("post_title", ""), "post_context": metadata.get("post_context", ""),
                "post_published_at_raw": metadata.get("post_published_at_raw", ""),
                "post_published_at": metadata.get("post_published_at", ""),
                "verdict": verdict, "reason": reason,
                "metadata_resolved": int(bool(metadata.get("metadata_resolved"))),
                "matched_groups": ",".join(matched_groups(f"{metadata.get('post_title', '')} {metadata.get('post_context', '')}")),
                "co_thanh_pho_khac": int(co_thanh_pho_khac(metadata.get("post_title", ""), metadata.get("post_context", ""))),
                "discovery_source": source.get("label", ""), "discovery_query": source.get("query", ""),
                "state": "accepted" if verdict == "accept" else "rejected",
                "crawled_at": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat(),
            }
            store.upsert_context(context)
            if verdict != "accept":
                continue
            if not _contamination_ok(post_url, comments):
                store.set_context_state("facebook", identity, "quarantined")
                store.kv_set("facebook_status", "quarantined")
                store.log_event("contamination", "facebook", {"url": post_url})
                store.complete_task(task["id"])
                store.kv_set("facebook_seen_posts", list(seen))
                return {"status": "quarantined", "url": post_url}
            records = _to_records(store, post_url, post_context, metadata, comments, run["run_id"], batch_id)
            store.add_comments(records)
            captured_count = store.comment_count_for_context("facebook", identity)
            expected_count, missing_count, capture_status = _comment_reconciliation(
                metadata.get("comment_capture", {}), captured_count, max_comments
            )
            prior = store.context("facebook", identity)
            prior_retries = prior.get("comment_retry_count", 0) if isinstance(prior, dict) else 0
            retry_count = int(prior_retries or 0)
            needs_retry = capture_status in ("incomplete", "unverified")
            if needs_retry:
                retry_count += 1
            context.update({
                "expected_comment_count": expected_count,
                "captured_comment_count": captured_count,
                "missing_comment_count": missing_count,
                "comment_capture_status": capture_status,
                "comment_retry_count": retry_count,
                "state": "comments_done" if capture_status == "complete" else f"comments_{capture_status}",
                "crawled_at": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat(),
            })
            store.upsert_context(context)
            store.log_event("fb_comment_reconciliation", "facebook", {
                "url": context["source_url"], "expected": expected_count,
                "captured": captured_count, "missing": missing_count,
                "status": capture_status, "retry_count": retry_count,
            })
            if needs_retry and retry_count <= _MAX_COMMENT_RECONCILIATION_RETRIES:
                _queue_reconciliation_retry(store, post_url, identity, retry_count)
                # Do not let the source-level seen set suppress the queued direct retry.
                seen.discard(identity)

        store.complete_task(task["id"])
        store.kv_set("facebook_seen_posts", list(seen))
        time.sleep(fb.DELAY_BETWEEN_SOURCES)
