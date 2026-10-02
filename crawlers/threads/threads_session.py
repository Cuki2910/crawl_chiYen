"""Threads browser session -- persistent authenticated profile + login-wall
detection, same pattern as crawl_tiktok.py's launch_context()/is_login_wall().

Threads (unlike TikTok) hides search results and most reply threads from
logged-out browsers almost immediately, so a real crawl run assumes an
already-logged-in profile. This module does NOT automate the login itself
(Threads' login flow goes through Instagram/Facebook credentials and may
require 2FA) -- run this file directly with --headful once, log in by hand
in the opened window, and the session persists in --user-data-dir for every
later discover_threads.py / crawl_threads.py run.

NOTE: the CSS selectors and login-wall URL markers below are best-effort,
written from Threads' general app shape (a Next.js/React web app under
threads.net, redirecting to a login page when a session is missing/expired).
They have not been confirmed against live Threads traffic -- verify them
against the real site before relying on this in an unattended run, and
tighten LOGIN_WALL_MARKERS/CAPTCHA_SELECTORS if they under- or over-match.
"""

import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

try:
    from patchright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - fallback path
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - stdlib-only unit tests must still import this module
        sync_playwright = None

ROOT = "https://www.threads.net"
OUTPUT_DIR = "data/outputs/threads"
USER_DATA_DIR = os.path.join(OUTPUT_DIR, ".browser_profile")

LOGIN_WALL_MARKERS = ("/login", "/accounts/login")
CAPTCHA_SELECTORS = [
    "div[class*='captcha']",
    "#captcha_container",
]


class BlockedError(Exception):
    """CAPTCHA or soft block detected. Caller must stop the whole run."""


class SessionExpiredError(Exception):
    """Redirected to login. Caller must stop the run and re-run this script
    headful to refresh the session."""


def is_login_wall(url):
    return any(marker in url for marker in LOGIN_WALL_MARKERS)


def check_block_markers(page):
    if is_login_wall(page.url):
        raise SessionExpiredError(f"Redirected to login: {page.url}")
    for selector in CAPTCHA_SELECTORS:
        if page.query_selector(selector):
            raise BlockedError(f"CAPTCHA marker matched: {selector} on {page.url}")


@contextmanager
def launch_context(headless=False, user_data_dir=None):
    """Yield (page, context). Caller drives navigation; the persistent
    profile (cookies, fingerprint) carries the logged-in session across
    every call and every future run using the same --user-data-dir."""
    if sync_playwright is None:
        raise RuntimeError(
            "Missing dependency: patchright/playwright. Run "
            "`pip install -r requirements.txt` then `patchright install chromium`."
        )
    user_data_dir = user_data_dir or USER_DATA_DIR
    os.makedirs(user_data_dir, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir,
            headless=headless,
            viewport={"width": 1280, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            yield page, context
        finally:
            context.close()


_LOGIN_PROMPT_TEXT_JS = """
() => {
    const text = (document.body ? document.body.innerText : '').slice(0, 4000);
    return /log in|đăng nhập|continue with instagram|create new account|tạo tài khoản mới|sign up/i.test(text);
}
"""


def is_logged_in(page):
    """Best-effort check: not on a login-wall URL, not on instagram.com
    (Threads auth redirects there mid-login -- never treat that as
    "logged in", it's the auth flow in progress), and no login/signup
    prompt text visible on the page.

    Any error reading the page (destroyed JS context from a mid-check
    navigation, cross-origin restrictions while instagram.com is loading,
    anything else) is treated as "not logged in yet, keep polling" rather
    than crashing -- the login flow bounces across origins and transient
    page states are expected, not exceptional, during this window."""
    try:
        url = page.url
    except Exception:
        return False
    if is_login_wall(url) or "instagram.com" in url:
        return False
    try:
        return not page.evaluate(_LOGIN_PROMPT_TEXT_JS)
    except Exception:
        return False


def interactive_login(user_data_dir=None):
    """Open a headful browser on threads.net for a one-time manual login.
    Run this directly: `python -m crawlers.threads.threads_session`.
    Login redirects through instagram.com -- that's expected, keep going
    until you land back on a logged-in threads.net page."""
    with launch_context(headless=False, user_data_dir=user_data_dir) as (page, _context):
        page.goto(f"{ROOT}/", wait_until="domcontentloaded", timeout=90000)
        print("[THREADS] Log in by hand in the opened browser window "
              "(this will redirect through instagram.com -- that's expected).")
        print("[THREADS] This script will keep polling until login is detected; "
              "close the window (Ctrl+C here) once you're done if it doesn't detect it.")
        while True:
            try:
                page.wait_for_timeout(3000)
            except Exception as exc:
                print(f"[THREADS] page unavailable, still waiting: {exc!r}")
                continue
            if is_logged_in(page):
                # One clean check isn't enough to trust given this
                # heuristic's history -- require two consecutive clean
                # reads a few seconds apart before declaring success.
                page.wait_for_timeout(3000)
                if is_logged_in(page):
                    print("THREADS_LOGIN_OK")
                    return
            try:
                status = "closed" if page.is_closed() else page.url
            except Exception:
                status = "unavailable"
            print(f"[THREADS] still waiting... (current page: {status})")


if __name__ == "__main__":
    interactive_login()
