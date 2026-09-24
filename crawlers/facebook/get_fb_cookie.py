"""Chạy 1 lần để lấy cookie Facebook: mở browser thật, tự đăng nhập trong cửa sổ
đó, script tự phát hiện khi đăng nhập xong (poll cookie c_user xuất hiện) rồi
ghi c_user/xs vào .env (không hardcode, không log giá trị xs ra console)."""

import sys
import time

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

ENV_PATH = ".env"
TIMEOUT_SECONDS = 300
POLL_INTERVAL = 3

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto("https://www.facebook.com/login")

    print(f"Đăng nhập Facebook trong cửa sổ vừa mở. Đang chờ tối đa {TIMEOUT_SECONDS}s...")

    by_name = {}
    waited = 0
    while waited < TIMEOUT_SECONDS:
        cookies = page.context.cookies("https://www.facebook.com")
        by_name = {c["name"]: c["value"] for c in cookies}
        if "c_user" in by_name and "xs" in by_name:
            print("Phát hiện đã đăng nhập.")
            break
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
    else:
        print("Hết thời gian chờ, chưa thấy đăng nhập xong.")

    browser.close()

if "c_user" not in by_name or "xs" not in by_name:
    print("KHÔNG lấy được cookie c_user/xs.")
else:
    with open(ENV_PATH, "a", encoding="utf-8") as f:
        f.write(f"FB_COOKIE_C_USER={by_name['c_user']}\n")
        f.write(f"FB_COOKIE_XS={by_name['xs']}\n")
    print(f"Đã lưu cookie vào {ENV_PATH}. KHÔNG commit file này (đã có trong .gitignore).")
