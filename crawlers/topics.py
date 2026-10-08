"""Topic gate for Hanoi flood and transport content."""

import re
import unicodedata

from crawlers.hanoi_flood_transport.keywords import GROUPS

REQUIRED_GROUPS = ("flood_state", "transport_impact", "hanoi_location")
MATCH_GROUPS = (*REQUIRED_GROUPS, "electric_bus")


def _norm(text):
    return unicodedata.normalize("NFC", text or "")


def _to_regex(entry):
    pattern = entry["term"]
    return pattern if entry["type"] == "regex" else r"(?<!\w)" + re.escape(pattern) + r"(?!\w)"


GROUP_RX = {
    group: re.compile("|".join(_to_regex(entry) for entry in GROUPS[group]), re.IGNORECASE)
    for group in MATCH_GROUPS
}
_OTHER_CITY = re.compile(
    r"hồ chí minh|tp\.? ?hcm|sài gòn|đà nẵng|da nang|hải phòng|hai phong|"
    r"cần thơ|can tho|nha trang|huế|hạ long|đà lạt|da lat|hà nam|ha nam",
    re.IGNORECASE,
)


def matched_groups(text):
    text = _norm(text)
    return [group for group in MATCH_GROUPS if GROUP_RX[group].search(text)]


def co_thanh_pho_khac(title="", body=""):
    """Flag explicit non-Hanoi city mentions for downstream review."""
    return bool(_OTHER_CITY.search(f"{_norm(title)} {_norm(body)}"))


def gate_post(title="", body=""):
    """Accept only when flood, transport, and Hanoi evidence all exist."""
    blob = f"{_norm(title)} {_norm(body)}"
    missing = [group for group in REQUIRED_GROUPS if not GROUP_RX[group].search(blob)]
    if missing:
        return "reject", "missing_group:" + "+".join(missing)
    return "accept", "flood_transport_hanoi"


def gate_electric_bus(title="", body=""):
    """Accept posts with an approved electric-bus term."""
    blob = f"{_norm(title)} {_norm(body)}"
    if not GROUP_RX["electric_bus"].search(blob):
        return "reject", "missing_group:electric_bus"
    return "accept", "electric_bus"


_VN_EVIDENCE = re.compile(
    r"[ăắằẳẵặấầẩẫậềếểễệồốổỗộơờớởỡợưừứửữựđảạẻẹẽỉịỏọủụỷỵỹ]|"
    r"(?<!\w)(?:vietnam|viet nam|hanoi|ha noi|saigon|ho chi minh|vinbus|vinfast|vingroup|"
    r"xanh sm|tp\.? ?hcm|da nang)(?!\w)",
    re.IGNORECASE,
)


def gate_electric_bus_vietnam(title="", body=""):
    """Electric-bus gate plus Vietnam evidence (Vietnamese text or VN place/brand)."""
    verdict, reason = gate_electric_bus(title, body)
    if verdict != "accept":
        return verdict, reason
    if not _VN_EVIDENCE.search(f"{_norm(title)} {_norm(body)}"):
        return "reject", "not_vietnam"
    return verdict, reason


def is_relevant(title="", body=""):
    return gate_post(title, body)[0] == "accept"


# --------------------------------------------------------------------------
# TikTok/News compatibility wrappers -- added when the TikTok/News crawlers
# (originally a separate repo, their own gate/keyword module transcribed
# independently from the same source spreadsheet) were merged in here.
# gate_post() already takes two text blobs and concatenates them, so both
# wrappers below just reshape their platform-specific inputs into that same
# call rather than duplicating any gate logic.
# --------------------------------------------------------------------------


def is_on_topic_tiktok(caption, hashtags, source=None):
    """TikTok wrapper: caption + a plain space-joined hashtag blob (hashtags
    are matched as plain substrings by gate_post()'s word-boundary regex,
    so no separate normalization step is needed here). ``source`` accepted
    for call-site compatibility, not consulted."""
    hashtag_blob = " ".join((hashtags or []))
    return gate_post(caption or "", hashtag_blob)


def is_on_topic_news(title, body="", source=None):
    """News wrapper: same shape as gate_post(), just the platform-neutral
    name the News crawler's call sites expect. ``source`` accepted for
    call-site compatibility, not consulted."""
    return gate_post(title, body)


def is_on_topic_threads(post_text, hashtags=None, source=None):
    """Threads wrapper: post text + a plain space-joined hashtag blob, same
    shape as is_on_topic_tiktok(). ``source`` accepted for call-site
    compatibility, not consulted."""
    hashtag_blob = " ".join((hashtags or []))
    return gate_post(post_text or "", hashtag_blob)


def is_on_topic_tiktok_electric_bus(caption, hashtags, source=None):
    """TikTok wrapper for the electric-bus campaign -- same shape as
    is_on_topic_tiktok(), gated through gate_electric_bus_vietnam()
    (electric-bus term match plus Vietnam evidence, same gate the
    YouTube electric-bus campaign uses) instead of gate_post().
    ``source`` accepted for call-site compatibility, not consulted."""
    hashtag_blob = " ".join((hashtags or []))
    return gate_electric_bus_vietnam(caption or "", hashtag_blob)


def is_on_topic_news_electric_bus(title, body="", source=None):
    """News wrapper for the electric-bus campaign -- same shape as
    is_on_topic_news(), gated through gate_electric_bus_vietnam()
    (electric-bus term match plus Vietnam evidence, same gate the
    YouTube electric-bus campaign uses) instead of gate_post().
    ``source`` accepted for call-site compatibility, not consulted."""
    return gate_electric_bus_vietnam(title, body)


def is_on_topic_threads_electric_bus(post_text, hashtags=None, source=None):
    """Threads wrapper for the electric-bus campaign -- same shape as
    is_on_topic_threads(), gated through gate_electric_bus_vietnam()
    (electric-bus term match plus Vietnam evidence, same gate the
    YouTube electric-bus campaign uses) instead of gate_post().
    ``source`` accepted for call-site compatibility, not consulted."""
    hashtag_blob = " ".join((hashtags or []))
    return gate_electric_bus_vietnam(post_text or "", hashtag_blob)
