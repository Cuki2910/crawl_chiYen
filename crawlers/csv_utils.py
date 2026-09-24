"""Helper dùng chung cho các crawler ghi output CSV (facebook, youtube).

Vì sao tách riêng: 3 crawler (crawl_facebook, crawl_youtube, crawl_curated) từng copy-paste
y hệt logic flatten + header-check này. Sửa 1 chỗ, chỗ khác quên sửa là bug chờ sẵn.
"""
import csv
import os


def load_env():
    """Tự động đọc các biến trong file .env ở thư mục gốc vào os.environ (nếu chưa có)."""
    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def flatten_record(record):
    """Thay mọi newline (\\n, \\r\\n, \\r) trong field text bằng space.

    Cần thiết vì csv writer quote đúng chuẩn nhưng 1 record có \\n bên trong sẽ
    hiển thị thành nhiều dòng khi mở bằng text editor / một số trình xem CSV,
    dễ gây hiểu lầm là file bị hỏng.
    """
    return {
        k: v.replace("\r\n", " ").replace("\r", " ").replace("\n", " ") if isinstance(v, str) else v
        for k, v in record.items()
    }


def open_csv_writer(path, fieldnames):
    """Mở file CSV ở chế độ append, viết header nếu file chưa tồn tại hoặc rỗng.

    Trả về (file_handle, writer). Caller chịu trách nhiệm đóng file_handle
    (khuyến khích dùng trong `with`).
    """
    write_header = not os.path.exists(path) or os.path.getsize(path) == 0
    f = open(path, "a", encoding="utf-8-sig", newline="")
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    if write_header:
        writer.writeheader()
    return f, writer
