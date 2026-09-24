import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from scrapling import StealthyFetcher
from crawlers.facebook.crawl_facebook import _fb_cookies, NAV_TIMEOUT_MS

url = 'https://www.facebook.com/share/p/1EAk5RBHSd/'

def inspect(page):
    page.wait_for_timeout(5000)
    for i in range(20):
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1000)
    n0 = page.evaluate("""() => document.querySelectorAll('div[role="article"]').length""")
    print("Before clicking Xem tat ca:", n0)

    clicked = page.evaluate("""() => {
        const spans = [...document.querySelectorAll('span')];
        const target = spans.find(s => s.textContent.trim() === 'Xem tất cả');
        if (target) {
            let clickable = target.closest('[role="button"]') || target.closest('div') || target;
            clickable.click();
            return true;
        }
        return false;
    }""")
    print("Clicked 'Xem tat ca':", clicked)
    page.wait_for_timeout(4000)
    n1 = page.evaluate("""() => document.querySelectorAll('div[role="article"]').length""")
    print("After clicking Xem tat ca:", n1)

    for i in range(15):
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1200)
        page.evaluate("""() => {
            const buttons = [...document.querySelectorAll('[role="button"], button, span')];
            buttons.forEach(b => {
                const text = (b.getAttribute('aria-label') || b.textContent || '').toLowerCase().trim();
                if (text.includes('xem thêm bình luận') || (text.includes('xem') && text.includes('phản hồi')) || text.includes('view more') || text.includes('repl')) {
                    try { b.click(); } catch(e){}
                }
            });
        }""")
        page.wait_for_timeout(1500)
    n2 = page.evaluate("""() => document.querySelectorAll('div[role="article"]').length""")
    print("After 15 more rounds scroll+expand:", n2)
    return page

try:
    StealthyFetcher.fetch(url, headless=True, cookies=_fb_cookies(), network_idle=False,
        timeout=NAV_TIMEOUT_MS, block_ads=True, hide_canvas=True, block_webrtc=True, page_action=inspect)
except Exception as e:
    print("Error:", e)
