"""Topic configuration shared by the YouTube and Facebook crawlers."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from crawlers.hanoi_flood_transport.query_gen import generate_queries

HCM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
UTC = timezone.utc
SOURCES_VERSION = "hanoi_flood_transport_sources_v1_2026-09"


def post_windows(now=None):
    """Return the two requested publication windows in Vietnam time."""
    now = now or datetime.now(HCM_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=HCM_TZ)
    now = now.astimezone(HCM_TZ)
    return (
        (datetime(2025, 8, 1, tzinfo=HCM_TZ), datetime(2025, 11, 1, tzinfo=HCM_TZ)),
        (datetime(2026, 8, 1, tzinfo=HCM_TZ), now),
    )


def _parse_timestamp(value):
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Facebook's absolute tooltip timestamps omit the offset but are local HCM time.
        parsed = parsed.replace(tzinfo=HCM_TZ)
    return parsed.astimezone(HCM_TZ)


def post_in_window(value, now=None):
    published = _parse_timestamp(value)
    if published is None:
        return False
    return any(start <= published < end for start, end in post_windows(now))


def youtube_windows(now=None):
    """Return exclusive UTC API bounds for YouTube publishedAfter/Before."""
    for start, end in post_windows(now):
        yield (
            start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        )


TOPIC_QUERIES = tuple(generate_queries(max_queries=30))


def facebook_sources():
    sources = []
    for index, query in enumerate(TOPIC_QUERIES, 1):
        sources.extend((
            {
                "kind": "search",
                "label": f"flood_post_{index:02d}",
                "source_class": "topic_search",
                "query": query,
                "max_posts": 8,
            },
            {
                "kind": "video_search",
                "label": f"flood_video_{index:02d}",
                "source_class": "topic_search",
                "query": query,
                "max_posts": 8,
            },
        ))
    return sources
