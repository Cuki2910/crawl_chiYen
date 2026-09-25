"""Facebook browser session/auth cho baseline_gate_v2."""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from crawlers.csv_utils import load_env

load_env()

try:
    from scrapling.fetchers import StealthySession
except ImportError:
    StealthySession = None

PROFILE_DIR = os.path.abspath(".secrets/facebook_profile")
CREDENTIALS_FILE = os.path.abspath(".secrets/facebook.env")
CHALLENGE_SHOT = os.path.abspath(".secrets/auth_challenge.png")
NAV_TIMEOUT_MS = 90000

AUTH_OK = "ok"
AUTH_LOGIN_FORM = "login_form"
AUTH_CHALLENGE = "challenge"


class FacebookAuthRequiredError(RuntimeError):
    def __init__(self, state=AUTH_LOGIN_FORM):
        self.state = state
        super().__init__(state)


class FacebookProfileBusyError(RuntimeError):
    pass


def load_credentials():
    if not os.path.exists(CREDENTIALS_FILE):
        raise RuntimeError(f"Thiếu file credentials: {CREDENTIALS_FILE}")
    creds = {}
    with open(CREDENTIALS_FILE, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                creds[key.strip()] = value.strip()
    email = creds.get("FB_LOGIN_EMAIL", "")
    password = creds.get("FB_LOGIN_PASSWORD", "")
    if not email or not password:
        raise RuntimeError("Credentials thiếu FB_LOGIN_EMAIL hoặc FB_LOGIN_PASSWORD")
    return email, password


def load_session_cookies():
    """Return optional Facebook cookies from .env for an already-authenticated session."""
    user_id = os.environ.get("FB_COOKIE_C_USER", "").strip()
    session_key = os.environ.get("FB_COOKIE_XS", "").strip()
    if not user_id or not session_key:
        return None
    return [
        {"name": "c_user", "value": user_id, "domain": ".facebook.com", "path": "/"},
        {"name": "xs", "value": session_key, "domain": ".facebook.com", "path": "/"},
    ]


def has_persistent_cookies(profile_dir):
    return any(
        os.path.exists(os.path.join(profile_dir, relative_path))
        for relative_path in ("Default/Network/Cookies", "Default/Cookies")
    )


def _classify_auth_js():
    return r"""
    () => {
        const url = location.href;
        const text = document.body ? document.body.innerText.slice(0, 4000) : '';
        const hasCheckpoint = /\/checkpoint\//.test(url) ||
            !!document.querySelector('input[name="captcha_response"]') ||
            /xác nhận danh tính|confirm your identity|security check|kiểm tra bảo mật|mã đăng nhập|login code/i.test(text);
        const loginForm = /\/login(?:\/|\?|$)/.test(url) ||
            (!!document.querySelector('input[name="email"]') && !!document.querySelector('input[name="pass"]'));
        const loggedIn = !!document.querySelector('[aria-label="Facebook"]') ||
            !!document.querySelector('div[role="banner"]') ||
            !!document.querySelector('[aria-label*="Trang chủ"], [aria-label*="Home"]');
        let state = 'ok';
        if (hasCheckpoint) state = 'challenge';
        else if (loginForm || !loggedIn) state = 'login_form';
        document.documentElement.setAttribute('data-auth-state', state);
        return state;
    }
    """


def _blocking_auth_js():
    return r"""
    () => {
        const url = location.href;
        const text = document.body ? document.body.innerText.slice(0, 4000) : '';
        const challenge = /\/checkpoint\//.test(url) ||
            !!document.querySelector('input[name="captcha_response"]') ||
            /xác nhận danh tính|confirm your identity|security check|kiểm tra bảo mật|mã đăng nhập|login code/i.test(text);
        const login = /\/login(?:\/|\?|$)/.test(url) ||
            (!!document.querySelector('input[name="email"]') && !!document.querySelector('input[name="pass"]'));
        const state = challenge ? 'challenge' : (login ? 'login_form' : 'ok');
        document.documentElement.setAttribute('data-auth-state', state);
        return state;
    }
    """


def _is_profile_busy(exc):
    message = str(exc).lower()
    return "processsingleton" in message or "profile is already in use" in message or "profile directory" in message and "in use" in message


def _auth_state(page):
    try:
        return page.evaluate(_classify_auth_js())
    except Exception as exc:
        if "execution context was destroyed" in str(exc).lower():
            return AUTH_LOGIN_FORM
        raise


def _continue_saved_profile(page):
    try:
        return bool(page.evaluate(r"""
            () => {
                const nodes = [...document.querySelectorAll('[role="button"], button, [aria-label]')];
                const target = nodes.find(node => {
                    const label = (node.getAttribute('aria-label') || '').trim();
                    return /^(continue|tiếp tục)(\s|$)/i.test(label);
                });
                if (!target) return false;
                target.click();
                return true;
            }
        """))
    except Exception as exc:
        if "execution context was destroyed" in str(exc).lower():
            return True
        raise


class FacebookSession:
    def __init__(self, profile_dir=PROFILE_DIR, session_factory=None, headless=True):
        self.profile_dir = profile_dir
        self.session_factory = session_factory or StealthySession
        self.headless = headless
        self.session = None
        self.auth_page = None
        self.owner_thread = None

    def start(self):
        if self.session is not None:
            return self
        if self.session_factory is None:
            raise RuntimeError("Thiếu scrapling; cài requirements trước khi crawl live.")
        os.makedirs(self.profile_dir, exist_ok=True)
        session_options = dict(
            headless=self.headless,
            user_data_dir=self.profile_dir,
            locale="vi-VN",
            network_idle=False,
            timeout=NAV_TIMEOUT_MS,
            block_ads=True,
            hide_canvas=True,
            block_webrtc=True,
            extra_flags=["--start-minimized"] if self.headless else [],
        )
        cookies = load_session_cookies()
        if cookies and not has_persistent_cookies(self.profile_dir):
            session_options["cookies"] = cookies
        session = self.session_factory(**session_options)
        try:
            session.start()
        except Exception as exc:
            try:
                session.close()
            except Exception:
                pass
            if _is_profile_busy(exc):
                raise FacebookProfileBusyError("Facebook profile đang được Chromium khác sử dụng") from None
            raise
        self.session = session
        self.owner_thread = threading.get_ident()
        return self

    def _check_thread(self):
        if self.owner_thread is not None and self.owner_thread != threading.get_ident():
            raise RuntimeError("FacebookSession không được chia sẻ giữa threads")

    def close(self):
        self._check_thread()
        if self.session is not None:
            self.session.close()
        self.session = None
        self.auth_page = None
        self.owner_thread = None

    def fetch(self, url, **kwargs):
        self.start()
        self._check_thread()
        return self.session.fetch(url, **kwargs)

    def _page(self):
        self.start()
        self._check_thread()
        if self.auth_page is None or self.auth_page.is_closed():
            self.auth_page = self.session.context.new_page()
            self.auth_page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            self.auth_page.set_default_timeout(NAV_TIMEOUT_MS)
        return self.auth_page

    def ensure_login(self):
        page = self._page()
        if page.url == "about:blank":
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
        state = _auth_state(page)
        if state == AUTH_OK:
            return state, {"challenge_shot": None}
        if state == AUTH_CHALLENGE:
            self._capture_challenge(page)
            return state, {"challenge_shot": CHALLENGE_SHOT}

        if state == AUTH_LOGIN_FORM and _continue_saved_profile(page):
            page.wait_for_timeout(5000)
            state = _auth_state(page)
            if state == AUTH_OK:
                return state, {"challenge_shot": None}

        try:
            email, password = load_credentials()
        except RuntimeError as exc:
            return state, {"challenge_shot": None, "auth_error": str(exc)}

        if not page.locator('input[name="email"]').count():
            page.goto("https://www.facebook.com/login/", wait_until="domcontentloaded")
        page.wait_for_selector('input[name="email"]', timeout=30000)
        page.fill('input[name="email"]', email)
        page.fill('input[name="pass"]', password)
        # Enter trên password tránh giữ locator submit cũ qua navigation/2FA redirect.
        page.locator('input[name="pass"]').press("Enter")
        page.wait_for_timeout(10000)
        state = _auth_state(page)
        if state != AUTH_OK:
            self._capture_challenge(page)
        return state, {"challenge_shot": CHALLENGE_SHOT if state == AUTH_CHALLENGE else None}

    @staticmethod
    def _capture_challenge(page):
        try:
            page.screenshot(path=CHALLENGE_SHOT)
        except Exception:
            pass


def ensure_login(session=None):
    browser = session or FacebookSession()
    browser.start()
    return browser.ensure_login()


def interactive_login():
    browser = FacebookSession(headless=False).start()
    try:
        while True:
            state, _ = browser.ensure_login()
            if state == AUTH_OK:
                print("FACEBOOK_LOGIN_OK", flush=True)
                return
            print("FACEBOOK_AUTH_REQUIRED: xử lý CAPTCHA/checkpoint trong cửa sổ đang mở.", flush=True)
            browser.auth_page.wait_for_timeout(2000)
    finally:
        browser.close()


if __name__ == "__main__":
    if "--interactive" in sys.argv:
        interactive_login()
    else:
        browser = FacebookSession().start()
        try:
            state, detail = browser.ensure_login()
            print(f"auth_state={state}")
            if detail.get("challenge_shot"):
                print(f"challenge screenshot: {detail['challenge_shot']}")
        finally:
            browser.close()
