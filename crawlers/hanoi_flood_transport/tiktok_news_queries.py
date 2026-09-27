"""TikTok/News search-query list for the Hanoi flood-transport topic.

Separate from query_gen.py's generate_queries() (used by the Facebook
crawler) on purpose: TikTok's search and News' on-site search behave
differently enough from Facebook's post search that a shared automatic
generator wasn't a good fit -- this list was built by hand, combining terms
from keywords.py's 3 groups directly (a bare flood_state term like "ngập"
alone is too broad/nationwide; combining with a location or transport term
narrows it). The GATE both platforms run against (crawlers/topics.py) is
still the single shared one -- only the search-query construction differs
per platform, matching how Facebook and YouTube already keep their own
separate keyword lists in this codebase.

Structure: 15 general/impact/mode queries, then 85 "ngập <location>"
queries -- one per literal entry in keywords.py's HANOI_LOCATION group.
"""

KEYWORDS = (
    # General flood + Hanoi
    "ngập lụt Hà Nội", "ngập nước Hà Nội", "Hà Nội ngập", "đường ngập Hà Nội",
    "mưa ngập Hà Nội",
    # Flood + traffic/commute impact
    "ngập lụt giao thông Hà Nội", "kẹt xe ngập nước Hà Nội",
    "tắc đường ngập Hà Nội", "ùn tắc ngập Hà Nội",
    "giao thông tê liệt ngập Hà Nội",
    # Flood + specific commute/mode
    "ngập đi làm Hà Nội", "ngập đi học Hà Nội", "xe máy ngập nước Hà Nội",
    "ô tô ngập nước Hà Nội", "xe buýt ngập Hà Nội",
    # "ngập <location>" -- one per keywords.py HANOI_LOCATION literal entry
    "ngập Hà Nội", "ngập Ha Noi", "ngập Thủ đô",
    "ngập Hoàn Kiếm", "ngập Ba Đình", "ngập Đống Đa", "ngập Hai Bà Trưng",
    "ngập Cầu Giấy", "ngập Thanh Xuân", "ngập Hoàng Mai", "ngập Hà Đông",
    "ngập Tây Hồ", "ngập Long Biên", "ngập Nam Từ Liêm", "ngập Bắc Từ Liêm",
    "ngập Thanh Trì", "ngập Gia Lâm", "ngập Đông Anh", "ngập Sóc Sơn",
    "ngập Hoài Đức", "ngập Đan Phượng", "ngập Thường Tín", "ngập Thanh Oai",
    "ngập Quốc Oai", "ngập Chương Mỹ", "ngập Mê Linh", "ngập Phúc Thọ",
    "ngập Thạch Thất", "ngập Sơn Tây", "ngập Ba Vì", "ngập Mỹ Đức",
    "ngập Ứng Hòa", "ngập Phú Xuyên",
    "ngập Mỹ Đình", "ngập Cầu Diễn", "ngập Nhổn", "ngập Xuân Phương",
    "ngập Tây Mỗ", "ngập Đại Mỗ", "ngập Trung Văn", "ngập Mễ Trì",
    "ngập Yên Hòa", "ngập Dịch Vọng", "ngập Nghĩa Đô", "ngập Kim Giang",
    "ngập Định Công", "ngập Linh Đàm", "ngập Đại Kim", "ngập Yên Sở",
    "ngập Văn Quán", "ngập Mỗ Lao", "ngập La Khê", "ngập Dương Nội",
    "ngập An Khánh", "ngập Vinhomes Smart City", "ngập Vinhomes Ocean Park",
    "ngập Nguyễn Trãi", "ngập Nguyễn Xiển", "ngập Khuất Duy Tiến",
    "ngập Nguyễn Tuân", "ngập Quan Nhân", "ngập Vũ Trọng Phụng",
    "ngập Tố Hữu", "ngập Lê Văn Lương", "ngập Trần Duy Hưng",
    "ngập Phạm Hùng", "ngập Dương Đình Nghệ", "ngập Trần Bình",
    "ngập Hồ Tùng Mậu", "ngập Xuân Thủy", "ngập Phạm Văn Đồng",
    "ngập Hoàng Quốc Việt", "ngập Thụy Khuê", "ngập Đội Cấn",
    "ngập Minh Khai", "ngập Tam Trinh", "ngập Lĩnh Nam",
    "ngập Nguyễn Chính", "ngập Giải Phóng", "ngập Ngọc Hồi",
    "ngập Quang Trung", "ngập Yên Nghĩa", "ngập Đại lộ Thăng Long",
    "ngập Pháp Vân", "ngập Cầu Bươu",
)
