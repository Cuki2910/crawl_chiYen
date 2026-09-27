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


def is_logged_in(page):
    """Best-effort check: not on a login wall after landing on the home feed."""
    return not is_login_wall(page.url)


def interactive_login(user_data_dir=None):
    """Open a headful browser on threads.net for a one-time manual login.
    Run this directly: `python -m crawlers.threads.threads_session`."""
    with launch_context(headless=False, user_data_dir=user_data_dir) as (page, _context):
        page.goto(f"{ROOT}/", wait_until="domcontentloaded", timeout=90000)
        print("[THREADS] Log in by hand in the opened browser window.")
        print("[THREADS] This script will keep polling until the login wall clears; "
              "close the window (Ctrl+C here) once you're done if it doesn't detect it.")
        while True:
            page.wait_for_timeout(3000)
            if is_logged_in(page):
                print("THREADS_LOGIN_OK")
                return
            print("[THREADS] still on login wall, waiting...")


if __name__ == "__main__":
    interactive_login()
