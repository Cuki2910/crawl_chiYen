"""YouTube Data API v3 client dùng chung — search video theo keyword, lấy comment.

Chỉ chứa API client thuần (search/fetch); ghi output và vòng lặp crawl nằm ở
youtube_worker.py (baseline_gate_v2). Không có CLI standalone ở đây nữa —
tránh 2 code path ghi cùng 1 file output với 2 schema khác nhau.

Retention: dữ liệu comment lấy qua API phải xóa/refresh trong 30 ngày kể từ
`crawled_at` (YouTube API Services Developer Policies). Xem
data/outputs/youtube/README.md.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from crawlers.csv_utils import load_env
from crawlers.hanoi_flood_transport.config import TOPIC_QUERIES

load_env()
API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
if not API_KEY:
    raise RuntimeError("Thiếu YOUTUBE_API_KEY trong môi trường hoặc file .env")
# Nhiều key (YOUTUBE_API_KEY, YOUTUBE_API_KEY_2, ...) mỗi key có quota search
# 100/ngày riêng (theo project GCP) — hết key này thì xoay sang key kế thay vì
# chờ reset. _api_get tự inject/xoay key hiện tại, call site không cần biết.
_API_KEYS = [API_KEY]
_i = 2
while True:
    extra = os.environ.get(f"YOUTUBE_API_KEY_{_i}", "")
    if not extra:
        break
    _API_KEYS.append(extra)
    _i += 1
_key_index = 0
BASE_URL = "https://www.googleapis.com/youtube/v3"

# KEYWORDS v1 — cập nhật 2026-07-22, dùng cho đợt search P0/P1.
# Sửa list này khi đổi giai đoạn nghiên cứu; ghi lại ngày cập nhật ở trên.
KEYWORDS = [
    # Nhóm chính sách miễn phí & ứng dụng (P0/P1)
    "xe buýt miễn phí TP.HCM",
    "VNeID xe buýt",
    "MultiGo xe buýt",
    "xe buýt miễn phí 1/7/2026",
    "vé tháng xe buýt miễn phí TP.HCM",
    "hoàn tiền vé tập xe buýt",
    "thẻ xe buýt miễn phí TP.HCM",
    "MultiGo xe buýt TP.HCM",
    "thanh toán vé xe buýt MultiGo",
    "VNeID xe buýt TP.HCM",
    # Nhóm mở rộng: Trải nghiệm, review & dịch vụ buýt TP.HCM / Sài Gòn (tối đa hoá dữ liệu)
    "xe buýt Sài Gòn",
    "xe buýt TP HCM",
    "buýt điện TP HCM",
    "xe buýt điện Sài Gòn",
    "VinBus TP HCM",
    "thử đi xe buýt Sài Gòn",
    "review xe buýt Sài Gòn",
    "đi xe bus TPHCM",
    "xe buýt công cộng TPHCM",
    "buýt Sài Gòn trải nghiệm",
    "xe bus miễn phí Sài Gòn",
    "xe bus Sài Gòn 2026",
    # Nhóm P2: định danh bắt buộc (1/10-31/12/2026) — VNeID/CCCD/MultiGo, RQ3 nhóm yếu thế
    "xe buýt VNeID bắt buộc",
    "xe buýt định danh CCCD",
    "xe buýt xác thực VNeID MultiGo",
    "quét mã xe buýt VNeID",
    "MultiGo xe buýt định danh",
    "người già xe buýt VNeID",
    "người cao tuổi không có smartphone xe buýt",
    "xe buýt miễn phí khó khăn định danh",
    "xe buýt miễn phí tháng 10 định danh",
    "xe buýt 1/10 VNeID",
]
KEYWORDS = list(TOPIC_QUERIES)

search_call_count = 0
comment_page_count = 0
_last_call_at = 0.0
MIN_CALL_INTERVAL_SECONDS = 3.0

# Ước lượng quota units theo endpoint (server mới là nguồn sự thật; đây chỉ để báo tiến độ).
_QUOTA_COST = {"search": 100, "commentThreads": 1, "comments": 1, "videos": 1}
estimated_quota_used = 0


def reset_counters():
    """Reset global counters — worker gọi mỗi lần khởi động/resume trong cùng process."""
    global search_call_count, comment_page_count, estimated_quota_used, _last_call_at
    search_call_count = 0
    comment_page_count = 0
    estimated_quota_used = 0
    _last_call_at = 0.0


def _api_get(path, params):
    """Gọi API. Throttle mọi endpoint — burst không delay giữa các call là nguyên
    nhân chính gây rateLimitExceeded (không phải quota ngày).

    Tự inject key hiện tại; khi gặp quota ngày đã cạn, xoay sang key kế trong
    _API_KEYS và retry 1 lần trước khi coi là hết quota thật (mọi key đều cạn)."""
    global estimated_quota_used, _last_call_at, _key_index
    tried = 0
    while True:
        wait = MIN_CALL_INTERVAL_SECONDS - (time.monotonic() - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        call_params = dict(params, key=_API_KEYS[_key_index])
        url = f"{BASE_URL}/{path}?{urllib.parse.urlencode(call_params)}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read())
            estimated_quota_used += _QUOTA_COST.get(path, 1)
            return data
        except HTTPError as e:
            body = json.loads(e.read())
            error_info = body.get("error", {}).get("errors", [{}])[0]
            reason = error_info.get("reason", "unknown")
            # API trả reason="rateLimitExceeded" cả cho quota NGÀY đã cạn (message chứa
            # "per day") lẫn burst tạm thời. Coi nhầm loại đầu là burst khiến worker retry
            # sau 15-60p vô ích cả ngày — quota ngày chỉ reset lúc nửa đêm Pacific.
            if reason == "rateLimitExceeded" and "per day" in error_info.get("message", "").lower():
                reason = "quotaExceeded"
            if reason == "quotaExceeded" and tried < len(_API_KEYS) - 1:
                _key_index = (_key_index + 1) % len(_API_KEYS)
                tried += 1
                continue
            raise YouTubeAPIError(e.code, reason, body) from None
        finally:
            _last_call_at = time.monotonic()


class YouTubeAPIError(Exception):
    def __init__(self, status, reason, body):
        self.status = status
        self.reason = reason
        self.body = body
        super().__init__(f"{status} {reason}")


def search_videos(keyword, published_after, published_before, max_results=50, max_pages=3, max_calls=75):
    global search_call_count
    videos = []
    page_token = None
    for _ in range(max_pages):
        if max_calls is not None and search_call_count >= max_calls:
            break
        params = {
            "part": "snippet",
            "type": "video",
            "q": keyword,
            "relevanceLanguage": "vi",
            "order": "date",
            "maxResults": max_results,
            "publishedAfter": published_after,
            "publishedBefore": published_before,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            data = _api_get("search", params)
            search_call_count += 1
        except YouTubeAPIError as e:
            if e.reason in ("quotaExceeded", "rateLimitExceeded"):
                raise
            print(f"[LỖI SEARCH PAGE] keyword={keyword!r} reason={e.reason}")
            break
        for item in data.get("items", []):
            snippet = item["snippet"]
            videos.append({
                "video_id": item["id"]["videoId"],
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "published_at": snippet.get("publishedAt", ""),
            })
        page_token = data.get("nextPageToken")
        if not page_token or (max_calls is not None and search_call_count >= max_calls):
            break
    return videos


def fetch_video_comment_counts(video_ids):
    """Lấy commentCount cho tối đa 50 video bằng videos.list (1 quota unit)."""
    if not video_ids:
        return {}
    if len(video_ids) > 50:
        raise ValueError("videos.list nhận tối đa 50 IDs mỗi batch")
    data = _api_get("videos", {
        "part": "statistics",
        "id": ",".join(video_ids),
        "maxResults": 50,
    })
    counts = {}
    for item in data.get("items", []):
        try:
            counts[item["id"]] = int(item.get("statistics", {}).get("commentCount", 0))
        except (TypeError, ValueError):
            counts[item["id"]] = 0
    return counts


def fetch_video_descriptions(video_ids):
    """Lấy description ĐẦY ĐỦ qua videos.list — search.list trả description bị API cắt ngắn."""
    if not video_ids:
        return {}
    if len(video_ids) > 50:
        raise ValueError("videos.list nhận tối đa 50 IDs mỗi batch")
    data = _api_get("videos", {
        "part": "snippet",
        "id": ",".join(video_ids),
        "maxResults": 50,
    })
    return {item["id"]: item.get("snippet", {}).get("description", "") for item in data.get("items", [])}


def fetch_comments(video_id):
    global comment_page_count
    page_token = None
    while True:
        params = {
            "part": "snippet",
            "videoId": video_id,
            "textFormat": "plainText",
            "maxResults": 100,
        }
        if page_token:
            params["pageToken"] = page_token
        data = _api_get("commentThreads", params)
        comment_page_count += 1
        for item in data.get("items", []):
            yield item
        page_token = data.get("nextPageToken")
        if not page_token:
            break


def fetch_replies(parent_id):
    """Lấy TOÀN BỘ reply của một top-level comment; embedded replies không đảm bảo đủ."""
    page_token = None
    while True:
        params = {
            "part": "snippet",
            "parentId": parent_id,
            "textFormat": "plainText",
            "maxResults": 100,
        }
        if page_token:
            params["pageToken"] = page_token
        data = _api_get("comments", params)
        for item in data.get("items", []):
            yield item
        page_token = data.get("nextPageToken")
        if not page_token:
            break
