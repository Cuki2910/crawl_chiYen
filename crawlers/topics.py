"""Topic gate for Hanoi flood and transport content."""

import re
import unicodedata

from crawlers.hanoi_flood_transport.keywords import GROUPS

REQUIRED_GROUPS = ("flood_state", "transport_impact", "hanoi_location")


def _norm(text):
    return unicodedata.normalize("NFC", text or "")


def _to_regex(entry):
    pattern = entry["term"]
    return pattern if entry["type"] == "regex" else r"(?<!\w)" + re.escape(pattern) + r"(?!\w)"


GROUP_RX = {
    group: re.compile("|".join(_to_regex(entry) for entry in GROUPS[group]), re.IGNORECASE)
    for group in REQUIRED_GROUPS
}
_OTHER_CITY = re.compile(
    r"hồ chí minh|tp\.? ?hcm|sài gòn|đà nẵng|da nang|hải phòng|hai phong|"
    r"cần thơ|can tho|nha trang|huế|hạ long|đà lạt|da lat|hà nam|ha nam",
    re.IGNORECASE,
)


def matched_groups(text):
    text = _norm(text)
    return [group for group in REQUIRED_GROUPS if GROUP_RX[group].search(text)]


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
