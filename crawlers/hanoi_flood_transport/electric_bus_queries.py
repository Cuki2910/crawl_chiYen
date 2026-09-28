"""TikTok/News search-query list for the Hanoi electric-bus topic.

Search-side only -- widens recall. The gate that actually accepts/rejects
candidates is `gate_electric_bus()` (crawlers/topics.py), which matches
only against the original `ELECTRIC_BUS` term list in keywords.py. Nothing
here is a gate term: a query below can return a candidate whose caption
never contains one of the original terms, and that candidate will still be
rejected by the gate -- these queries exist purely to surface more
candidates for the gate to filter, the same discover-then-gate split every
other platform in this repo already uses.

Structure: the 26 unique literal terms from keywords.py's ELECTRIC_BUS
group (`literal_terms("electric_bus")`, deduped -- the raw list has 5
duplicate entries), followed by 20 broader/rephrased queries (routes,
fares, comparisons, complaints/praise, VinBus branding) added to widen
recall beyond the gate's exact wording.
"""

from crawlers.hanoi_flood_transport.keywords import literal_terms

_BASE_TERMS = tuple(literal_terms("electric_bus"))

_EXPANDED_QUERIES = (
    "đi xe buýt điện Hà Nội",
    "trạm sạc xe buýt điện",
    "giá vé xe buýt điện",
    "lộ trình VinBus",
    "tuyến VinBus Hà Nội",
    "cảm nhận đi VinBus",
    "sự cố xe buýt điện",
    "xe buýt điện có tốt không",
    "chuyến xe buýt điện đầu tiên",
    "trải nghiệm đi VinBus",
    "xe điện VinFast công cộng",
    "buýt điện thông minh",
    "dự án xe buýt điện Hà Nội",
    "chính sách vé xe buýt điện",
    "so sánh xe buýt điện xe buýt thường",
    "lịch trình xe buýt điện",
    "hình ảnh xe buýt điện",
    "video xe buýt điện",
    "phản hồi xe buýt điện",
    "xe buýt điện VinFast Hà Nội",
)


def _dedup_preserve_order(terms):
    seen, out = set(), []
    for term in terms:
        key = term.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(term.strip())
    return out


KEYWORDS = tuple(_dedup_preserve_order(_BASE_TERMS + _EXPANDED_QUERIES))
