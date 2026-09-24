"""Facebook public-post crawler.

Legacy output is intentionally not repaired in place. Clean records emitted by this
crawler carry ``capture_scope == "target_permalink_article"`` plus provenance.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from crawlers.csv_utils import flatten_record, open_csv_writer, load_env
from crawlers.facebook.facebook_session import FacebookAuthRequiredError, _blocking_auth_js
from crawlers.hanoi_flood_transport.config import SOURCES_VERSION as TOPIC_SOURCES_VERSION, facebook_sources, post_in_window
from crawlers.topics import co_thanh_pho_khac, gate_post, matched_groups

load_env()

try:
    # StealthyFetcher dùng undetected Chrome (Patchright) — khó bị Facebook detect hơn
    # DynamicFetcher so sánh. Giữ lại import DynamicFetcher để unit test fallback.
    from scrapling.fetchers import StealthyFetcher
except ImportError:  # Allows stdlib-only unit tests to import helpers.
    StealthyFetcher = None

ROOT = "https://www.facebook.com"
OUTPUT_DIR = "data/outputs/facebook"
OUTPUT_FILE = "facebook_comments.csv"  # 1 file cố định, luôn append + dedup theo id
AUDIT_FILE = "facebook_post_audit.csv"
AUDIT_FIELDS = ["source_url", "post_title", "post_context", "post_published_at_raw", "post_published_at", "metadata_resolved", "verdict", "reason", "discovery_query", "matched_groups", "co_thanh_pho_khac", "error", "crawled_at"]
MAX_COMMENTS_PER_POST = 1000
SCROLL_ROUNDS = 15
POST_SCROLL_ROUNDS = 45
DELAY_BETWEEN_SOURCES = 20
NAV_TIMEOUT_MS = 90000
CAPTURE_SCOPE = "target_permalink_article"
REEL_CAPTURE_SCOPE = "target_reel_comments"
VIDEO_CAPTURE_SCOPE = "target_video_comments"

SOURCES_VERSION = "facebook_sources_v3_2026-07-24"
SOURCES = [
    {
        "kind": "public_page",
        "label": "gtcctphcm",
        "source_class": "official_page",
        "url": "https://www.facebook.com/gtcctphcm/",
        "max_posts": 12,
    },
    {
        "kind": "search",
        "label": "free_fare_hcm",
        "source_class": "policy_search",
        "query": "xe buýt miễn phí TP.HCM",
        "max_posts": 10,
    },
    {
        "kind": "search",
        "label": "vneid_bus_hcm",
        "source_class": "policy_search",
        "query": "VNeID xe buýt TP.HCM",
        "max_posts": 10,
    },
    {
        "kind": "search",
        "label": "multigo_bus_hcm",
        "source_class": "service_search",
        "query": "MultiGo xe buýt TP.HCM",
        "max_posts": 10,
    },
    {
        "kind": "search",
        "label": "commute_bus_hcm",
        "source_class": "everyday_search",
        "query": "đi bus đi làm đi học khám bệnh TP.HCM",
        "max_posts": 8,
    },
    {
        "kind": "search",
        "label": "delay_crowding_hcm",
        "source_class": "service_search",
        "query": "xe buýt TP.HCM chờ lâu đông xe trạm tuyến",
        "max_posts": 8,
    },
    {
        "kind": "search",
        "label": "access_barriers_hcm",
        "source_class": "access_search",
        "query": "xe buýt TP.HCM CCCD VNeID điện thoại thẻ ngân hàng người lớn tuổi khuyết tật thu nhập thấp",
        "max_posts": 8,
    },
    # --- Video / Reel sources ---
    # Keywords mới: người tạo Reel/Video dùng ngôn ngữ ngắn, catchy khác hẳn post text.
    # Format phổ biến: hướng dẫn (tutorial), review, thử trải nghiệm, vlog.
    {
        "kind": "video_search",
        "label": "video_guide_bus",
        "source_class": "policy_search",
        "query": "hướng dẫn đi xe buýt miễn phí",
        "max_posts": 8,
    },
    {
        "kind": "video_search",
        "label": "video_review_bus",
        "source_class": "everyday_search",
        "query": "review xe buýt Sài Gòn",
        "max_posts": 8,
    },
    {
        "kind": "video_search",
        "label": "video_try_bus",
        "source_class": "everyday_search",
        "query": "thử đi xe buýt Sài Gòn",
        "max_posts": 8,
    },
    {
        "kind": "video_search",
        "label": "video_electric_bus",
        "source_class": "service_search",
        "query": "xe buýt điện Sài Gòn",
        "max_posts": 8,
    },
    {
        "kind": "video_search",
        "label": "video_multigo_guide",
        "source_class": "service_search",
        "query": "hướng dẫn MultiGo xe buýt",
        "max_posts": 8,
    },
    {
        "kind": "video_search",
        "label": "video_free_bus_short",
        "source_class": "policy_search",
        "query": "xe bus miễn phí",
        "max_posts": 8,
    },
]

SOURCES_VERSION = TOPIC_SOURCES_VERSION
SOURCES = facebook_sources()

SELECTORS = {
    "feed_items": 'div[role="feed"] > div',
    "direct_post_link": re.compile(r"/posts/|/permalink/|/videos/|story_fbid=|/watch/|/reel/"),
    "photo_link": re.compile(r"/photo/"),
    "resolved_permalink": re.compile(r"/posts/(pfbid|\d)|story_fbid=|/permalink/|/videos/|/reel/|/watch/"),
}

_EXPAND_BUTTON_PATTERN = re.compile(
    r"Xem thêm|View more|xem .* bình luận|xem .* phản hồi|xem .* câu trả lời|"
    r"\d+ (repl(y|ies)|comments?)",
    re.IGNORECASE,
)
_TARGET_MARK_ATTR = "data-scraped-target"
_COMMENT_DATA_ATTR = "data-scraped-comments"
_COMMENT_STATS_ATTR = "data-scraped-comment-stats"
_TARGET_RESOLVED_ATTR = "data-scraped-target-resolved"
_METADATA_DATA_ATTR = "data-scraped-post-metadata"
_ACTION_STATE_ATTR = "data-scraped-action-state"

_CAPTURE_POST_METADATA_JS = r"""
() => {
    let cleanTitle = (document.title || '')
        .replace(/^\(\d+\)\s*/, '')
        .replace(/\s*\|\s*Facebook\s*$/, '')
        .split(/\s+-\s+/)[0]
        .trim();
    if (/^facebook$/i.test(cleanTitle)) cleanTitle = '';
    const norm = text => (text || '').toLocaleLowerCase().replace(/\s+/g, ' ').trim();
    const titleWords = norm(cleanTitle).split(' ').filter(word => word.length >= 3);
    const score = text => {
        const value = norm(text);
        return titleWords.reduce((sum, word) => sum + (value.includes(word) ? 1 : 0), 0);
    };
    const isTopLevel = article => !article.parentElement || !article.parentElement.closest('div[role="article"]');
    const articles = [...document.querySelectorAll('div[role="article"]')].filter(isTopLevel);
    let article = articles
        .filter(node => (node.innerText || '').trim().length >= 20)
        .sort((a, b) => score(b.innerText) - score(a.innerText))[0] || null;
    const titleNodes = [...document.querySelectorAll('div[dir="auto"]')]
        .filter(node => (node.innerText || '').trim().length >= 8)
        .sort((a, b) => score(b.innerText) - score(a.innerText));
    const titleNode = titleNodes[0] || null;
    const articleScore = article ? score(article.innerText) : 0;
    const titleScore = titleNode ? score(titleNode.innerText) : 0;
    let body = '';
    let scope = article;
    // Facebook permalink desktop có thể render bài chính ngoài role=article và chỉ
    // dùng role=article cho comment. Khi đó node dir=auto khớp document.title là
    // định danh hẹp nhất, an toàn hơn article đầu tiên hoặc toàn bộ role=main.
    if (titleNode && titleScore >= Math.max(2, articleScore)) {
        body = (titleNode.innerText || titleNode.textContent || '').trim();
        scope = titleNode.closest('[role="main"]') || titleNode.parentElement;
    } else if (article && articleScore >= 2) {
        const bodyParts = [...article.querySelectorAll('div[dir="auto"]')]
            .filter(node => !node.closest('div[role="article"] div[role="article"]'))
            .filter(node => !node.closest('[role="button"], [role="toolbar"]'))
            .map(node => (node.innerText || node.textContent || '').trim())
            .filter(text => text.length >= 3);
        body = [...new Set(bodyParts)].join('\n').trim();
    }
    if (!body) {
        document.documentElement.setAttribute('%s', JSON.stringify({resolved: false, title: cleanTitle, body: '', published_at_raw: ''}));
        return;
    }
    if (!scope) scope = document;
    const timeCandidates = [...scope.querySelectorAll('time, abbr, a[href], [role="tooltip"], [data-tooltip-content], [data-tooltip-text]')];
    const publishedAtCandidates = [];
    for (const node of timeCandidates) {
        const href = node.getAttribute('href') || '';
        const looksLikePermalink = /\/posts\/|\/permalink\/|story_fbid=|\/videos\/|\/reel\/|\/watch\//.test(href);
        if (!node.matches('time,abbr,[role="tooltip"],[data-tooltip-content],[data-tooltip-text]') && !looksLikePermalink) continue;
        const values = [
            node.getAttribute('datetime'), node.getAttribute('title'), node.getAttribute('aria-label'),
            node.getAttribute('data-tooltip-content'), node.getAttribute('data-tooltip-text'), node.textContent,
        ].filter(Boolean).map(value => value.trim()).filter(Boolean);
        for (const value of values) {
            if (!publishedAtCandidates.includes(value)) publishedAtCandidates.push(value);
        }
    }
    document.documentElement.setAttribute('%s', JSON.stringify({
        resolved: true, title: cleanTitle, body,
        published_at_candidates: publishedAtCandidates.slice(0, 100),
        published_at_raw: publishedAtCandidates[0] || '',
    }));
}
""" % (_METADATA_DATA_ATTR, _METADATA_DATA_ATTR)

_INIT_COMMENT_CAPTURE_JS = r"""
(args) => {
    const targetUrl = args.targetUrl;
    const expectedBody = (args.expectedBody || '').toLocaleLowerCase().replace(/\s+/g, ' ').trim();
    const markAttr = '%s';
    const resolvedAttr = '%s';
    const identity = raw => {
        const u = new URL(raw, location.origin);
        const path = u.pathname.replace(/\/+$/, '');
        for (const rx of [/\/posts\/([^/]+)$/, /\/videos\/([^/]+)$/, /\/reel\/([^/]+)$/]) {
            const match = path.match(rx);
            if (match) return match[1];
        }
        return u.searchParams.get('story_fbid') || u.searchParams.get('fbid') || u.searchParams.get('v') || path;
    };
    const targetId = identity(targetUrl);
    const commentContextIds = [...new Set([...document.querySelectorAll('a[href]')]
        .map(link => link.href || '')
        .filter(href => /comment_id=|reply_comment_id=/.test(href) && /\/posts\/|story_fbid=|\/permalink\//.test(href))
        .map(identity))];
    const targetPath = new URL(targetUrl, location.origin).pathname.replace(/\/+$/, '');
    const isPhotoTarget = targetPath === '/photo';
    const locationMatches = identity(location.href) === targetId;
    const mappedIdentity = commentContextIds.length === 1 ? commentContextIds[0] : '';
    const mappingValid = !!mappedIdentity && (isPhotoTarget || mappedIdentity === targetId);
    const captureIdentity = mappingValid ? mappedIdentity : targetId;
    const bodyText = (document.body ? document.body.innerText : '').toLocaleLowerCase().replace(/\s+/g, ' ').trim();
    const bodyProbe = expectedBody.slice(0, Math.min(expectedBody.length, 120));
    const bodyMatches = bodyProbe.length >= 20 && bodyText.includes(bodyProbe);
    const explicitEmpty = /chưa có bình luận nào|no comments yet/i.test(bodyText);
    const isTopLevel = article => !article.parentElement || !article.parentElement.closest('div[role="article"]');
    const topLevel = [...document.querySelectorAll('div[role="article"]')].filter(isTopLevel);
    let matches = topLevel.filter(article => [...article.querySelectorAll('a[href]')].some(link => {
        const href = link.href || '';
        return !/comment_id=|reply_comment_id=/.test(href) && identity(href) === targetId;
    }));
    // Permalink desktop thường render post body ngoài role=article; role=article chỉ
    // là comments. Flat mode chỉ hợp lệ khi URL hiện tại + metadata body khớp và
    // comment permalinks có đúng một context identity (post: phải bằng target).
    const flatMapped = locationMatches && mappingValid && (isPhotoTarget || bodyMatches);
    if (matches.length === 0 && flatMapped) {
        matches = topLevel.filter(article => {
            const text = (article.innerText || '').toLocaleLowerCase().replace(/\s+/g, ' ').trim();
            return bodyProbe.length >= 20 && text.includes(bodyProbe);
        });
    }

    document.querySelectorAll('div[' + markAttr + ']').forEach(el => el.removeAttribute(markAttr));
    window.__scrapedComments = {};
    window.__scrapedCommentStats = {};
    window.__targetUrl = targetUrl;
    window.__targetIdentity = captureIdentity;
    window.__flatTarget = matches.length === 0 && flatMapped;
    window.__explicitEmptyTarget = locationMatches && explicitEmpty && commentContextIds.length === 0;
    const resolved = matches.length === 1 || window.__flatTarget || window.__explicitEmptyTarget;
    document.documentElement.setAttribute(resolvedAttr, resolved ? '1' : '0');
    if (matches.length === 1) matches[0].setAttribute(markAttr, '1');
}
""" % (_TARGET_MARK_ATTR, _TARGET_RESOLVED_ATTR)

_CAPTURE_COMMENTS_JS = r"""
() => {
    if (document.documentElement.getAttribute('%s') !== '1') return;
    const comments = window.__scrapedComments || {};
    const target = document.querySelector('div[%s="1"]');
    const flatTarget = !!window.__flatTarget;
    const explicitEmptyTarget = !!window.__explicitEmptyTarget;
    if (explicitEmptyTarget) return;
    if (!target && !flatTarget) return;
    const identity = raw => {
        const u = new URL(raw, location.origin);
        const path = u.pathname.replace(/\/+$/, '');
        for (const rx of [/\/posts\/([^/]+)$/, /\/videos\/([^/]+)$/, /\/reel\/([^/]+)$/]) {
            const match = path.match(rx);
            if (match) return match[1];
        }
        return u.searchParams.get('story_fbid') || u.searchParams.get('fbid') || u.searchParams.get('v') || path;
    };

    // --- Strategy: detect nested vs flat DOM structure ---
    // DynamicFetcher/Playwright: Facebook renders comments NESTED inside post article.
    // StealthyFetcher/real Chrome: Facebook renders ALL articles as top-level SIBLINGS.
    const scopedCommentSelector = 'div[aria-label*="B\u00ecnh lu\u1eadn d\u01b0\u1edbi t\u00ean"], div[aria-label*="Comment by"]';
    const preferred = target ? [...target.querySelectorAll(scopedCommentSelector)] : [];
    const nestedFallback = target ? [...target.querySelectorAll('div[role="article"]')].filter(b => b !== target) : [];

    let blocks;
    if (preferred.length) {
        // Nested: aria-label comment blocks inside target
        blocks = preferred;
    } else if (nestedFallback.length) {
        // Nested: generic article blocks inside target
        blocks = nestedFallback;
    } else {
        // Flat structure: require explicit comment permalink evidence. Suggested posts
        // without comment_id/reply_comment_id never enter capture.
        const isTopLevel = (a) => !a.parentElement || !a.parentElement.closest('div[role="article"]');
        blocks = [...document.querySelectorAll('div[role="article"]')]
            .filter(isTopLevel)
            .filter(block => [...block.querySelectorAll('a[href]')]
                .some(link => /comment_id=|reply_comment_id=/.test(link.href || '')));
    }

    const authorOf = block => {
        let label = block.getAttribute('aria-label') || '';
        if (!/Bình luận dưới tên|Comment by/i.test(label)) {
            const nested = block.querySelector('[aria-label*="Bình luận dưới tên"], [aria-label*="Comment by"]');
            label = nested ? (nested.getAttribute('aria-label') || '') : '';
        }
        const m = label.match(/(?:Bình luận dưới tên|Comment by)\s+(.+)$/i);
        return m ? m[1].trim() : '';
    };

    blocks.forEach(block => {
        if ((target && block === target) || block.querySelector('h1,h2')) return;
        const belongsToBlock = node => {
            const article = node.closest('div[role="article"]');
            return !article || article === block || !block.contains(article);
        };
        const text = [...block.querySelectorAll('div[dir="auto"]')]
            .filter(belongsToBlock)
            .map(node => node.textContent.trim())
            .filter(Boolean)
            .join(' ')
            .trim()
            .replace(/\s*(Xem thêm|See more)\s*$/i, '')
            .trim();
        if (!text || text.length < 3) return;
        const identity = raw => {
            const u = new URL(raw, location.origin);
            const path = u.pathname.replace(/\/+$/, '');
            for (const rx of [/\/posts\/([^/]+)$/, /\/videos\/([^/]+)$/, /\/reel\/([^/]+)$/]) {
                const match = path.match(rx);
                if (match) return match[1];
            }
            return u.searchParams.get('story_fbid') || u.searchParams.get('fbid') || u.searchParams.get('v') || path;
        };
        const permalinkLink = [...block.querySelectorAll('a[href]')]
            .filter(belongsToBlock)
            .find(a => /comment_id=|reply_comment_id=/.test(a.href || '') &&
                identity(a.href || '') === window.__targetIdentity);
        if (!permalinkLink) return;
        // aria-label thường mirror text hiển thị (tương đối, vd "5 tuần") — KHÔNG parse
        // được thành mốc tuyệt đối. Giá trị tuyệt đối nằm ở title/data-tooltip-*, giống
        // cách post metadata đã phải đọc (xem _CAPTURE_POST_METADATA_JS).
        const postedAtRaw = permalinkLink.getAttribute('title') ||
            permalinkLink.getAttribute('data-tooltip-content') ||
            permalinkLink.getAttribute('data-tooltip-text') ||
            permalinkLink.getAttribute('aria-label') || '';
        if (!postedAtRaw.trim()) return;
        const permalinkUrl = new URL(permalinkLink.href, location.origin);
        const replyId = permalinkUrl.searchParams.get('reply_comment_id') || '';
        const topId = permalinkUrl.searchParams.get('comment_id') || '';
        const commentId = replyId || topId;
        const parentBlock = block.parentElement ? block.parentElement.closest('div[role="article"]') : null;
        const nestedReply = !!parentBlock && parentBlock !== target;
        const isReply = !!replyId || nestedReply;
        let parentId = replyId ? topId : '';
        if (nestedReply && !parentId) {
            const parentLink = [...parentBlock.querySelectorAll('a[href]')]
                .map(a => a.href || '').find(href => /comment_id=/.test(href)) || '';
            parentId = parentLink ? new URL(parentLink, location.origin).searchParams.get('comment_id') || '' : '';
        }
        const key = commentId || (text + '|' + postedAtRaw + '|' + parentId);
        comments[key] = {comment_text: text, posted_at_raw: postedAtRaw,
            comment_id: commentId, comment_type: isReply ? 'reply' : 'top_level',
            parent_comment_id: parentId, parent_unresolved: isReply && !parentId,
            author_raw: authorOf(block),
            post_url: window.__targetUrl || ''};
    });
    window.__scrapedComments = comments;
}
""" % (_TARGET_RESOLVED_ATTR, _TARGET_MARK_ATTR)


_STORE_COMMENTS_JS = """
() => {
    const scope = window.__commentScope || document.querySelector('div[%s="1"]') || document;
    const values = [];
    for (const match of (scope.innerText || '').matchAll(/(?:^|\\s)([\\d.,\\s]+)\\s*(?:b\\u00ecnh lu\\u1eadn|comments?)(?:\\s|$)/gi)) {
        const value = Number(match[1].replace(/[^\\d]/g, ''));
        if (Number.isSafeInteger(value)) values.push(value);
    }
    const text = scope.innerText || '';
    window.__scrapedCommentStats = {
        expected_total: values.length ? Math.max(...values) :
            (/ch\\u01b0a c\\u00f3 b\\u00ecnh lu\\u1eadn n\\u00e0o|no comments yet/i.test(text) ? 0 : null),
    };
    document.documentElement.setAttribute('%s', JSON.stringify(Object.values(window.__scrapedComments || {})));
    document.documentElement.setAttribute('%s', JSON.stringify(window.__scrapedCommentStats || {}));
}
""" % (_TARGET_MARK_ATTR, _COMMENT_DATA_ATTR, _COMMENT_STATS_ATTR)

_CAPTURE_COMMENT_STATS_JS = r"""
() => {
    const scope = window.__commentScope ||
        document.querySelector('div[%s="1"]') || document;
    const text = scope.innerText || '';
    const values = [];
    // The visible count is localized, but these labels cover the logged-in VN/EN UI.
    for (const match of text.matchAll(/(?:^|\s)([\d.,\s]+)\s*(?:b\u00ecnh lu\u1eadn|comments?)(?:\s|$)/gi)) {
        const value = Number(match[1].replace(/[^\d]/g, ''));
        if (Number.isSafeInteger(value)) values.push(value);
    }
    window.__scrapedCommentStats = {
        expected_total: values.length ? Math.max(...values) :
            (/ch\u01b0a c\u00f3 b\u00ecnh lu\u1eadn n\u00e0o|no comments yet/i.test(text) ? 0 : null),
    };
}
""" % _TARGET_MARK_ATTR

_INIT_REEL_COMMENT_CAPTURE_JS = r"""
(args) => {
    const targetUrl = args.targetUrl;
    const markAttr = '%s';
    const resolvedAttr = '%s';
    const identity = raw => {
        const u = new URL(raw, location.origin);
        const path = u.pathname.replace(/\/+$/, '');
        for (const rx of [/\/posts\/([^/]+)$/, /\/videos\/([^/]+)$/, /\/reel\/([^/]+)$/]) {
            const match = path.match(rx);
            if (match) return match[1];
        }
        return u.searchParams.get('story_fbid') || u.searchParams.get('fbid') || u.searchParams.get('v') || path;
    };
    // Reel player chưa render comments cho đến khi click nút Bình luận.
    const buttons = [...document.querySelectorAll('[role="button"], button')];
    const commentButton = buttons.find(el => /Bình luận|Comment/i.test(
        el.getAttribute('aria-label') || el.innerText || ''
    ));
    if (commentButton) commentButton.click();
    document.querySelectorAll('div[' + markAttr + ']').forEach(el => el.removeAttribute(markAttr));
    window.__scrapedComments = {};
    window.__scrapedCommentStats = {};
    window.__commentScope = null;
    window.__targetUrl = targetUrl;
    window.__targetIdentity = identity(targetUrl);
    document.documentElement.setAttribute(resolvedAttr,
        identity(location.href) === window.__targetIdentity ? '1' : '0');
}
""" % (_TARGET_MARK_ATTR, _TARGET_RESOLVED_ATTR)

_LOCATE_REEL_COMMENT_SCOPE_JS = """
() => {
    // Reel có 2 container cuộn độc lập: 1 pager chuyển giữa các Reel (KHÔNG chứa
    // comment), 1 panel comment riêng. Phải xác định đúng panel comment để
    // tránh cuộn nhầm sang Reel kế bên và gộp nhầm comment.
    if (window.__commentScope && document.contains(window.__commentScope)) return true;
    const article = document.querySelector('div[role="article"]');
    if (!article) return false;
    let node = article;
    while (node && node !== document.body) {
        const style = window.getComputedStyle(node);
        if ((style.overflowY === 'auto' || style.overflowY === 'scroll') && node.scrollHeight > node.clientHeight) {
            window.__commentScope = node;
            return true;
        }
        node = node.parentElement;
    }
    return false;
}
"""

_EXPAND_REEL_COMMENTS_JS = """
() => {
    const scope = window.__commentScope || document;
    const buttons = [...scope.querySelectorAll('[role="button"], button, span')];
    buttons.forEach(b => {
        const text = (b.getAttribute('aria-label') || b.textContent || '').toLowerCase().trim();
        if (text.includes('xem thêm bình luận') ||
            (text.includes('xem') && (text.includes('phản hồi') || text.includes('câu trả lời'))) ||
            text.includes('view more') || text.includes('repl') ||
            text === 'xem thêm' || text === 'see more') {
            const clickable = b.closest('[role="button"], button') || b;
            try { clickable.click(); } catch (e) {}
        }
    });
    if (window.__commentScope) {
        window.__commentScope.scrollBy({top: 3000, behavior: 'smooth'});
        window.__commentScope.dispatchEvent(new Event('scroll'));
    }
}
"""

_CAPTURE_REEL_COMMENTS_JS = r"""
() => {
    if (document.documentElement.getAttribute('%s') !== '1') return;
    const comments = window.__scrapedComments || {};
    // Chỉ đọc comment BÊN TRONG scope đã xác định, không đọc cả document để
    // tránh lẫn comment của Reel/video khác đang render sẵn bên cạnh.
    const scope = window.__commentScope || document;
    const authorOf = block => {
        let label = block.getAttribute('aria-label') || '';
        if (!/Bình luận dưới tên|Comment by/i.test(label)) {
            const nested = block.querySelector('[aria-label*="Bình luận dưới tên"], [aria-label*="Comment by"]');
            label = nested ? (nested.getAttribute('aria-label') || '') : '';
        }
        const m = label.match(/(?:Bình luận dưới tên|Comment by)\s+(.+)$/i);
        return m ? m[1].trim() : '';
    };
    [...scope.querySelectorAll('div[role="article"]')].forEach(block => {
        if (block.querySelector('h1,h2')) return;
        const belongsToBlock = node => {
            const article = node.closest('div[role="article"]');
            return !article || article === block || !block.contains(article);
        };
        const text = [...block.querySelectorAll('div[dir="auto"]')]
            .filter(belongsToBlock)
            .map(node => node.textContent.trim()).filter(Boolean).join(' ').trim()
            .replace(/\s*(Xem thêm|See more)\s*$/i, '').trim();
        if (!text || text.length < 3) return;
        const identity = raw => {
            const u = new URL(raw, location.origin);
            const path = u.pathname.replace(/\/+$/, '');
            for (const rx of [/\/posts\/([^/]+)$/, /\/videos\/([^/]+)$/, /\/reel\/([^/]+)$/]) {
                const match = path.match(rx);
                if (match) return match[1];
            }
            return u.searchParams.get('story_fbid') || u.searchParams.get('fbid') || u.searchParams.get('v') || path;
        };
        const permalinkLink = [...block.querySelectorAll('a[href]')]
            .filter(belongsToBlock)
            .find(a => /comment_id=|reply_comment_id=/.test(a.href || '') &&
                identity(a.href || '') === window.__targetIdentity);
        if (!permalinkLink) return;
        const postedAtRaw = permalinkLink.getAttribute('title') ||
            permalinkLink.getAttribute('data-tooltip-content') ||
            permalinkLink.getAttribute('data-tooltip-text') ||
            permalinkLink.getAttribute('aria-label') || '';
        if (!postedAtRaw.trim()) return;
        const permalinkUrl = new URL(permalinkLink.href, location.origin);
        const replyId = permalinkUrl.searchParams.get('reply_comment_id') || '';
        const topId = permalinkUrl.searchParams.get('comment_id') || '';
        const commentId = replyId || topId;
        const parentBlock = block.parentElement ? block.parentElement.closest('div[role="article"]') : null;
        const nestedReply = !!parentBlock && parentBlock !== block;
        const isReply = !!replyId || nestedReply;
        let parentId = replyId ? topId : '';
        if (nestedReply && !parentId) {
            const parentLink = [...parentBlock.querySelectorAll('a[href]')]
                .map(a => a.href || '').find(href => /comment_id=/.test(href)) || '';
            parentId = parentLink ? new URL(parentLink, location.origin).searchParams.get('comment_id') || '' : '';
        }
        const key = commentId || (text + '|' + postedAtRaw + '|' + parentId);
        comments[key] = {comment_text: text, posted_at_raw: postedAtRaw,
            comment_id: commentId, comment_type: isReply ? 'reply' : 'top_level',
            parent_comment_id: parentId, parent_unresolved: isReply && !parentId,
            author_raw: authorOf(block),
            post_url: window.__targetUrl || ''};
    });
    window.__scrapedComments = comments;
}
""" % _TARGET_RESOLVED_ATTR

_TRANSPORT_TERMS = (
    "xe buýt", "xe bus", "bus", "tuyến", "trạm", "bến", "vé", "đông xe", "chờ lâu",
    "tài xế", "tiếp viên", "chất lượng", "multigo", "giao thông công cộng",
)
_HCM_TERMS = ("tp.hcm", "tp hcm", "hồ chí minh", "sài gòn", "tphcm", "thành phố hồ chí minh")
_POLICY_TERMS = (
    "miễn phí", "định danh", "vneid", "cccd", "căn cước", "thẻ ngân hàng",
    "điện thoại", "smartphone", "quyền riêng tư", "privacy",
)
_ACCESS_TERMS = ("người lớn tuổi", "người già", "khuyết tật", "thu nhập thấp", "yếu thế")
_REJECT_TERMS = (
    "du lịch", "tour", "đảo", "sale", "giảm giá", "giải trí", "showbiz", "ca sĩ",
    "bầu cử", "quốc hội", "đảng", "game", "livestream bán",
)
_TRANSPORT_SOURCE_CLASSES = {
    "official_page", "policy_search", "service_search", "everyday_search", "access_search", "transport_group"
}
_CREATION_STORY_MESSAGE_TEXT_PATTERN = re.compile(
    r'"creation_story"\s*:\s*\{.*?"message"\s*:\s*\{.*?"text"\s*:\s*("(?:\\.|[^"\\])*")',
    re.DOTALL,
)


class CookieExpiredError(Exception):
    pass


class FacebookCaptureError(RuntimeError):
    pass


class FacebookTargetUnresolvedError(FacebookCaptureError):
    pass


def _norm_text(text):
    text = unicodedata.normalize("NFC", text or "").casefold()
    return re.sub(r"\s+", " ", text).strip()


def _contains_any(text, terms):
    return any(term in text for term in terms)


def canonical_post_url(url):
    parsed = urllib.parse.urlparse(urllib.parse.urljoin(ROOT, url))
    query = urllib.parse.parse_qs(parsed.query)
    path = re.sub(r"/+$", "", parsed.path) or "/"
    keep_query = {}
    if path in ("/permalink.php", "/story.php"):
        for key in ("story_fbid", "id"):
            if query.get(key):
                keep_query[key] = query[key][0]
    elif path == "/photo" and query.get("fbid"):
        keep_query["fbid"] = query["fbid"][0]
    elif path == "/watch" and query.get("v"):
        keep_query["v"] = query["v"][0]
    clean_query = urllib.parse.urlencode(keep_query)
    return urllib.parse.urlunparse(("https", "www.facebook.com", path, "", clean_query, ""))


def canonical_post_identity(url):
    parsed = urllib.parse.urlparse(canonical_post_url(url))
    query = urllib.parse.parse_qs(parsed.query)
    path = re.sub(r"/+$", "", parsed.path)
    for pattern in (r"/posts/([^/]+)$", r"/videos/([^/]+)$", r"/reel/([^/]+)$"):
        match = re.search(pattern, path)
        if match:
            return match.group(1)
    for key in ("story_fbid", "fbid", "v"):
        if query.get(key):
            return query[key][0]
    return path

def content_type_for_url(url):
    parsed = urllib.parse.urlparse(canonical_post_url(url))
    query = urllib.parse.parse_qs(parsed.query)
    if re.search(r"/(?:reel|share/r)/[^/]+$", parsed.path):
        return "reel"
    if re.search(r"/(?:videos|share/v)/[^/]+$", parsed.path):
        return "video"
    return "video" if parsed.path == "/watch" and query.get("v") else "post"


def url_matches_target(href, target_url):
    return canonical_post_url(href) == canonical_post_url(target_url) or (
        canonical_post_identity(href) == canonical_post_identity(target_url)
    )


def _source_value(source, key, default=""):
    return source.get(key, default) if source else default


def extract_creation_story_message_text(html_content):
    if isinstance(html_content, bytes):
        html_content = html_content.decode("utf-8", errors="replace")
    for match in _CREATION_STORY_MESSAGE_TEXT_PATTERN.finditer(html_content or ""):
        try:
            text = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def is_on_topic(post_context, source=None):
    """Backward-compatible wrapper; discovery provenance intentionally ignored."""
    verdict, reason = gate_post("", post_context)
    return verdict == "accept", reason


def _scroll_page(rounds):
    def action(page):
        for _ in range(rounds):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)
        return page

    return action


def _scroll_and_expand_page(post_url, rounds, max_comments=None, expected_body=""):
    def action(page):
        is_video_page = content_type_for_url(post_url) in ("reel", "video")
        init_script = _INIT_REEL_COMMENT_CAPTURE_JS if is_video_page else _INIT_COMMENT_CAPTURE_JS
        capture_script = _CAPTURE_REEL_COMMENTS_JS if is_video_page else _CAPTURE_COMMENTS_JS
        # Chờ DOM render article thật sự trước khi chạy INIT
        try:
            page.wait_for_selector('div[role="article"]', timeout=30000)
            page.wait_for_timeout(4000)
        except Exception:
            pass
        page.evaluate(init_script, {
            "targetUrl": canonical_post_url(post_url),
            "expectedBody": expected_body,
        })
        if is_video_page:
            # Reel comments chỉ xuất hiện sau khi panel được mở.
            page.wait_for_timeout(4000)

        stale_rounds = 0
        last_count = -1
        for _ in range(rounds):
            page.evaluate(capture_script)
            current_count = page.evaluate(
                """() => Object.keys(window.__scrapedComments || {}).length"""
            )
            if max_comments is not None and current_count >= max_comments:
                break
            page.evaluate(_CAPTURE_COMMENT_STATS_JS)

            if is_video_page:
                # Xác định đúng panel chứa comment của CHÍNH post này (không phải
                # pager chuyển Reel), rồi chỉ click/cuộn bên trong nó.
                page.evaluate(_LOCATE_REEL_COMMENT_SCOPE_JS)
                page.evaluate(_EXPAND_REEL_COMMENTS_JS)
                page.wait_for_timeout(2000)
            else:
                # Cuộn thành nhiều bước nhỏ (giống người dùng thật) thay vì 1 nhát lớn —
                # cuộn giật cục dễ khiến Facebook throttle/không kịp lazy-render hết reply.
                for _ in range(6):
                    page.mouse.wheel(0, 350)
                    page.wait_for_timeout(220)
                # Click các nút mở rộng (Xem thêm bình luận, Xem phản hồi) bằng JS.
                # Nút "Xem X phản hồi" của reply lồng nhau render bằng <span>, KHÔNG có
                # role="button" — nếu chỉ quét role=button/button sẽ bỏ sót toàn bộ reply.
                page.evaluate("""() => {
                    const buttons = [...document.querySelectorAll('[role="button"], button, span')];
                    buttons.forEach(b => {
                        const text = (b.getAttribute('aria-label') || b.textContent || '').toLowerCase().trim();
                        if (text.includes('xem thêm bình luận') ||
                            (text.includes('xem') && (text.includes('phản hồi') || text.includes('câu trả lời'))) ||
                            text.includes('view more') || text.includes('repl') ||
                            text === 'xem thêm' || text === 'see more') {
                            const clickable = b.closest('[role="button"], button') || b;
                            try { clickable.click(); } catch (e) {}
                        }
                    });
                }""")
                page.wait_for_timeout(1800)

            # Dừng sớm nếu 5 vòng liên tiếp không tăng thêm comment nào — đã chạm
            # giới hạn lazy-load thật của Facebook cho phiên này, chạy thêm vô ích.
            current_count = page.evaluate(
                """() => Object.keys(window.__scrapedComments || {}).length"""
            )
            if current_count == last_count:
                stale_rounds += 1
                if stale_rounds >= 5:
                    break
            else:
                stale_rounds = 0
                last_count = current_count

        page.evaluate(capture_script)
        page.evaluate(_STORE_COMMENTS_JS)
        return page

    return action


def _fb_cookies():
    return [
        {"name": "c_user", "value": os.environ["FB_COOKIE_C_USER"], "domain": ".facebook.com", "path": "/"},
        {"name": "xs", "value": os.environ["FB_COOKIE_XS"], "domain": ".facebook.com", "path": "/"},
    ]


# Baseline worker inject một long-lived session; legacy CLI không inject và giữ fetcher cũ.
_BROWSER_SESSION = None


def use_browser_session(session):
    global _BROWSER_SESSION
    _BROWSER_SESSION = session


def use_persistent_profile(profile_dir):
    """Legacy compatibility; baseline worker dùng use_browser_session()."""
    return profile_dir


def _auth_guarded_action(page_action):
    def action(page):
        root = "document.documentElement"
        page.evaluate(f"() => {root}.setAttribute('{_ACTION_STATE_ATTR}', 'started')")
        state = page.evaluate(_blocking_auth_js())
        if state != "ok":
            page.evaluate(f"() => {root}.setAttribute('{_ACTION_STATE_ATTR}', 'auth')")
            return page
        try:
            if page_action:
                page_action(page)
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:160]}"
            page.evaluate("message => document.documentElement.setAttribute('%s', 'error:' + message)" % _ACTION_STATE_ATTR, message)
            return page
        page.evaluate(f"() => {root}.setAttribute('{_ACTION_STATE_ATTR}', 'ok')")
        return page
    return action


def _response_attr(response, name, default=""):
    html = response.css("html")
    return html[0].attrib.get(name, default) if html else default


def _response_auth_state(response):
    return _response_attr(response, "data-auth-state", "ok")


def _fetch(url, page_action=None, wait_selector=None):
    """Fetch với StealthyFetcher (undetected Chrome) + Scrapling optimizations:

    - StealthyFetcher: chống canvas/WebRTC fingerprint → Facebook khó detect bot.
    - disable_resources: bỏ qua ảnh/font/CSS/media → nhanh hơn ~30-40%.
    - block_ads: block ~3500 tracking domain → ít tín hiệu nhận dạng hơn.
    - hide_canvas: random noise canvas → chống Facebook canvas fingerprinting.
    - block_webrtc: chặn WebRTC IP leak.
    - wait_selector: chờ content xuất hiện thay vì fixed timeout.
    """
    if _BROWSER_SESSION is not None:
        kwargs = {
            "network_idle": False,
            "timeout": NAV_TIMEOUT_MS,
            "page_action": _auth_guarded_action(page_action),
        }
        if wait_selector:
            kwargs["wait_selector"] = wait_selector
            kwargs["wait_selector_state"] = "attached"
        response = _BROWSER_SESSION.fetch(url, **kwargs)
        state = _response_auth_state(response)
        if state != "ok":
            raise FacebookAuthRequiredError(state)
        action_state = _response_attr(response, _ACTION_STATE_ATTR)
        if not action_state.startswith("ok"):
            raise FacebookCaptureError(action_state or "missing_page_action_state")
        return response

    if StealthyFetcher is None:
        raise RuntimeError("Missing dependency: scrapling. Install project requirements before live crawling.")
    for attempt in range(1, 4):
        try:
            kwargs = dict(
                headless=True,
                cookies=_fb_cookies(),
                network_idle=False,
                timeout=NAV_TIMEOUT_MS,
                block_ads=True,
                hide_canvas=True,
                block_webrtc=True,
                page_action=page_action,
            )
            if wait_selector:
                kwargs["wait_selector"] = wait_selector
                kwargs["wait_selector_state"] = "attached"
            return StealthyFetcher.fetch(url, **kwargs)
        except Exception as exc:
            print(f"  [Attempt {attempt}/3] {type(exc).__name__}: {str(exc)[:120]}")
            if attempt < 3:
                time.sleep(10)
    raise RuntimeError(f"Fetch failed after 3 attempts: {url}")


def _post_urls_from_items(items, max_posts):
    post_urls = []
    seen_ids = set()
    for item in items:
        direct_href = None
        photo_href = None
        for link in item.css("a[href]"):
            href = link.attrib.get("href", "")
            if not href:
                continue
            if SELECTORS["direct_post_link"].search(href):
                direct_href = href
                break
            if photo_href is None and SELECTORS["photo_link"].search(href):
                photo_href = href
        if direct_href:
            post_url = canonical_post_url(direct_href)
        elif photo_href:
            # Giữ chính photo fbid làm target identity; không resolve qua link generic
            # /reel/ vì Facebook có thể điều hướng sang Reel không liên quan.
            photo_url = canonical_post_url(photo_href)
            post_url = photo_url if canonical_post_identity(photo_url) not in ("/photo", "/photo/") else None
        else:
            post_url = None
        if not post_url:
            continue
        identity = canonical_post_identity(post_url)
        if identity in ("/reel", "/videos", "/watch", "/posts", "/permalink"):
            continue
        if identity in seen_ids:
            continue
        seen_ids.add(identity)
        post_urls.append(post_url)
        if len(post_urls) >= max_posts:
            break
    return post_urls


def _resolve_permalink_via_photo(photo_href):
    resp = _fetch(
        urllib.parse.urljoin(ROOT, photo_href),
        page_action=_scroll_page(1),
        wait_selector='a[href*="/posts/"], a[href*="/permalink/"]',
    )
    for link in resp.css("a[href]"):
        href = link.attrib.get("href", "")
        text = link.text.strip() if link.text else ""
        if text in ("Xem bài viết", "View post") or SELECTORS["resolved_permalink"].search(href):
            return canonical_post_url(href)
    return None


def search_posts(query, max_posts):
    url = f"{ROOT}/search/posts/?q={urllib.parse.quote(query)}"
    # wait_selector: chờ feed thật sự hiện ra trước khi scrape
    resp = _fetch(url, page_action=_scroll_page(SCROLL_ROUNDS), wait_selector='div[role="feed"]')
    items = resp.css(SELECTORS["feed_items"])
    if not items:
        raise CookieExpiredError("No search posts found. Cookie may be expired.")
    return _post_urls_from_items(items, max_posts)

def search_videos(query, max_posts):
    url = f"{ROOT}/search/videos/?q={urllib.parse.quote(query)}"
    resp = _fetch(
        url,
        page_action=_scroll_page(SCROLL_ROUNDS),
        wait_selector='div[role="feed"], div[role="article"]',
    )
    items = resp.css(SELECTORS["feed_items"]) or resp.css('div[role="article"]')
    if not items:
        raise CookieExpiredError("No search videos found. Cookie may be expired.")
    # Giữ lại cả video (/videos/) lẫn reel (/reel/) — bỏ post thường
    return [
        u for u in _post_urls_from_items(items, max_posts)
        if content_type_for_url(u) in ("video", "reel")
        or re.search(r"/videos/|/reel/", u)
    ]


def page_posts(url, max_posts):
    resp = _fetch(
        url,
        page_action=_scroll_page(SCROLL_ROUNDS),
        wait_selector='div[role="feed"], div[role="article"]',
    )
    items = resp.css(SELECTORS["feed_items"]) or resp.css('div[role="article"]')
    return _post_urls_from_items(items, max_posts)


def discover_posts(source):
    if source["kind"] == "direct_post":
        return [canonical_post_url(source["url"])]
    if source["kind"] == "search":
        return search_posts(source["query"], source["max_posts"])
    if source["kind"] == "public_page":
        return page_posts(source["url"], source["max_posts"])
    if source["kind"] == "video_search":
        return search_videos(source["query"], source["max_posts"])
    raise ValueError(f"Unsupported source kind: {source['kind']}")


def _capture_post_metadata_page(page):
    try:
        page.wait_for_selector('div[role="article"]', timeout=30000)
        page.wait_for_timeout(2500)
    except Exception:
        return page
    page.evaluate(_CAPTURE_POST_METADATA_JS)
    return page


def parse_comment_tooltip(raw):
    """Parse tooltip tuyệt đối của chính comment; không suy đoán thời gian tương đối."""
    value = unicodedata.normalize("NFKC", raw or "")
    value = re.sub(r"\s+", " ", value).strip().casefold()
    match = re.fullmatch(
        r"(?:thứ (?:hai|ba|tư|năm|sáu|bảy)|chủ nhật),\s*"
        r"(\d{1,2}) tháng (\d{1,2}),\s*(\d{4}) lúc (\d{1,2}):(\d{2})",
        value,
    )
    if match:
        day, month, year, hour, minute = map(int, match.groups())
    else:
        english = re.fullmatch(
            r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday),\s*"
            r"(january|february|march|april|may|june|july|august|september|october|november|december) "
            r"(\d{1,2}),\s*(\d{4}) at (\d{1,2}):(\d{2}) (am|pm)",
            value,
        )
        if not english:
            return ""
        month_name, day, year, hour, minute, meridiem = english.groups()
        month = (
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ).index(month_name) + 1
        day, year, hour, minute = map(int, (day, year, hour, minute))
        if not 1 <= hour <= 12:
            return ""
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    try:
        return datetime(
            year, month, day, hour, minute, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")
        ).isoformat()
    except ValueError:
        return ""


def _parse_absolute_post_time(raw):
    """Chỉ chuẩn hóa timestamp tuyệt đối chắc chắn; giữ nguyên relative time ở raw."""
    value = (raw or "").strip()
    if not value:
        return ""
    patterns = (
        (r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?", None),
        (r"\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}", "%d/%m/%Y %H:%M"),
        (r"\d{1,2}\s+tháng\s+\d{1,2},?\s+\d{4}\s+lúc\s+\d{1,2}:\d{2}", "%d tháng %m, %Y lúc %H:%M"),
        (r"[A-Za-z]+\s+\d{1,2},\s*\d{4}\s+at\s+\d{1,2}:\d{2}\s*(?:AM|PM)", "%B %d, %Y at %I:%M %p"),
    )
    for pattern, fmt in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if not match:
            continue
        candidate = match.group(0).replace("Z", "+00:00")
        if fmt == "%d tháng %m, %Y lúc %H:%M":
            candidate = re.sub(r"(tháng\s+\d{1,2}),?", r"\1,", candidate, count=1)
        try:
            parsed = datetime.strptime(candidate, fmt) if fmt else datetime.fromisoformat(candidate)
            return parsed.isoformat()
        except ValueError:
            continue
    return ""


def _metadata_from_response(resp, post_url):
    html = resp.css("html")
    raw = html[0].attrib.get(_METADATA_DATA_ATTR, "{}") if html else "{}"
    try:
        captured = json.loads(raw)
    except json.JSONDecodeError:
        captured = {}
    creation_story = extract_creation_story_message_text(resp.html_content)
    title = captured.get("title", "").strip()
    body = creation_story or captured.get("body", "")
    published_candidates = captured.get("published_at_candidates", [])
    if not isinstance(published_candidates, list):
        published_candidates = []
    published_candidates = [str(value).strip() for value in published_candidates if str(value).strip()]
    if captured.get("published_at_raw") and captured["published_at_raw"] not in published_candidates:
        published_candidates.insert(0, captured["published_at_raw"])
    published_raw = next((value for value in published_candidates if _parse_absolute_post_time(value)), "")
    published_raw = published_raw or next(iter(published_candidates), "")
    return {
        "post_title": title[:500],
        "post_context": body.strip()[:10000],
        "post_published_at_raw": published_raw,
        "post_published_at": _parse_absolute_post_time(published_raw),
        "metadata_resolved": bool(body.strip() and (creation_story or captured.get("resolved"))),
    }


def fetch_post_metadata(post_url):
    """Pha nhẹ: lấy body thật từ article đích; không mở rộng/capture comment."""
    resp = _fetch(post_url, page_action=_capture_post_metadata_page)
    return _metadata_from_response(resp, post_url)


def gate_metadata(metadata):
    """Fail closed khi không xác định chắc body bài."""
    if not metadata.get("metadata_resolved"):
        return "reject", "metadata_unresolved"
    return gate_post(metadata.get("post_title", ""), metadata.get("post_context", ""))


def fetch_eligible_post(post_url, max_comments=MAX_COMMENTS_PER_POST):
    """Metadata → gate → comments; reject tuyệt đối không gọi comment fetch."""
    metadata = fetch_post_metadata(post_url)
    if not metadata.get("post_published_at"):
        return metadata, "reject", "date_unresolved", "", []
    if not post_in_window(metadata["post_published_at"]):
        return metadata, "reject", "out_of_window", "", []
    verdict, reason = gate_metadata(metadata)
    if verdict != "accept":
        return metadata, verdict, reason, "", []
    post_context, comments = fetch_post(post_url, max_comments=max_comments, metadata=metadata)
    return metadata, verdict, reason, post_context, comments


def fetch_post(post_url, max_comments=MAX_COMMENTS_PER_POST, metadata=None):
    # wait_selector được xử lý bên trong page_action để đảm bảo chạy TRƯỚC _INIT_COMMENT_CAPTURE_JS
    resp = _fetch(
        post_url,
        page_action=_scroll_and_expand_page(
            post_url, POST_SCROLL_ROUNDS, max_comments, (metadata or {}).get("post_context", "")
        ),
    )
    metadata = metadata or {}
    post_context = metadata.get("post_context", "")
    if not post_context:
        title_tags = resp.css("title")
        raw_title = title_tags[0].text.strip() if title_tags else ""
        post_context = raw_title.removesuffix("| Facebook").strip()[:500]
        if content_type_for_url(post_url) in ("video", "reel"):
            post_context = extract_creation_story_message_text(resp.html_content) or post_context
    html = resp.css("html")
    target_resolved = html[0].attrib.get(_TARGET_RESOLVED_ATTR) == "1" if html else False
    if not target_resolved:
        raise FacebookTargetUnresolvedError(f"target_unresolved:{canonical_post_url(post_url)}")
    raw_comments = html[0].attrib.get(_COMMENT_DATA_ATTR, "[]") if html else "[]"
    comments = json.loads(raw_comments)
    raw_stats = html[0].attrib.get(_COMMENT_STATS_ATTR, "{}") if html else "{}"
    try:
        capture_stats = json.loads(raw_stats)
    except json.JSONDecodeError:
        capture_stats = {}
    valid_comments = []
    for comment in comments:
        posted_at = parse_comment_tooltip(comment.get("posted_at_raw", ""))
        if not posted_at:
            continue
        comment["posted_at"] = posted_at
        valid_comments.append(comment)
    if max_comments is not None:
        valid_comments = valid_comments[:max_comments]
    expected_total = capture_stats.get("expected_total")
    metadata["comment_capture"] = {
        "expected_total": expected_total if isinstance(expected_total, int) and expected_total >= 0 else None,
        "rendered_count": len(comments),
        "valid_count": len(valid_comments),
        "invalid_timestamp_count": len(comments) - len(valid_comments),
        "limited_by_max_comments": max_comments is not None and len(comments) >= max_comments,
    }
    return post_context, valid_comments


def to_record(post_url, post_context, comment_data, batch_id, source=None, topic_rule=""):
    posted_at_raw = comment_data.get("posted_at_raw", "")
    posted_at = parse_comment_tooltip(posted_at_raw)
    if not posted_at:
        raise ValueError("comment timestamp tooltip missing or invalid")
    canonical_url = canonical_post_url(post_url)
    content_type = content_type_for_url(canonical_url)
    text = _norm_text(comment_data["comment_text"])
    raw_key = f"{canonical_url}|{text}|{_norm_text(comment_data.get('posted_at_raw', ''))}"
    comment_hash = hashlib.sha1(raw_key.encode("utf-8")).hexdigest()[:16]
    record = {
        "id": f"facebook_c_{comment_hash}",
        "platform": "facebook",
        "source_url": canonical_url,
        "post_context": post_context,
        "comment_text": comment_data["comment_text"],
        "posted_at_raw": posted_at_raw,
        "posted_at": posted_at,
        "likes_count": comment_data.get("likes_count", 0),
        "crawled_at": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat(),
        "crawl_batch_id": batch_id,
        "source_kind": _source_value(source, "kind"),
        "source_class": _source_value(source, "source_class"),
        "source_label": _source_value(source, "label"),
        "discovery_query": _source_value(source, "query"),
        "discovery_url": _source_value(source, "url"),
        "topic_rule": topic_rule,
        "matched_groups": ",".join(matched_groups(comment_data["comment_text"])),
        "co_thanh_pho_khac": co_thanh_pho_khac("", post_context),
        "post_published_at_raw": comment_data.get("post_published_at_raw", ""),
        "post_published_at": comment_data.get("post_published_at", ""),
        "content_type": content_type,
        "capture_scope": {"reel": REEL_CAPTURE_SCOPE, "video": VIDEO_CAPTURE_SCOPE}.get(content_type, CAPTURE_SCOPE),
        "sources_version": SOURCES_VERSION,
    }
    return record


RECORD_FIELDS = [
    "id", "platform", "source_url", "post_context", "comment_text",
    "posted_at_raw", "posted_at", "likes_count", "crawled_at", "crawl_batch_id",
    "source_kind", "source_class", "source_label", "discovery_query",
    "discovery_url", "topic_rule", "matched_groups", "co_thanh_pho_khac",
    "post_published_at_raw", "post_published_at", "content_type", "capture_scope",
    "sources_version",
]


def _selected_sources(labels, classes):
    sources = SOURCES
    if labels:
        labels = set(labels)
        sources = [s for s in sources if s["label"] in labels]
    if classes:
        classes = set(classes)
        sources = [s for s in sources if s["source_class"] in classes]
    return sources


def _load_existing_ids(path):
    """Đọc cột id của file output hiện có, để dedup xuyên suốt các lần chạy."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return set()
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != RECORD_FIELDS:
            raise ValueError(
                f"Existing Facebook output has an incompatible schema: {path}. "
                "Use a new --output-dir; legacy relative timestamps cannot be backfilled."
            )
        return {row["id"] for row in reader if row.get("id")}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--batch-id")
    parser.add_argument("--source-label", action="append", default=[])
    parser.add_argument("--source-class", action="append", default=[])
    parser.add_argument("--date", help="Unused; kept for CLI backward-compat.")
    args = parser.parse_args(argv)

    os.makedirs(args.output_dir, exist_ok=True)
    now = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
    batch_id = args.batch_id or f"facebook_{now.strftime('%Y%m%d_%H%M')}"
    out_path = os.path.join(args.output_dir, OUTPUT_FILE)
    sources = _selected_sources(args.source_label, args.source_class)

    seen_posts = set()
    written_ids = _load_existing_ids(out_path)
    if written_ids:
        print(f"[DEDUP] loaded {len(written_ids)} existing ids from {out_path}, sẽ bỏ qua comment trùng")
    stats = {s["label"]: {"discovered": 0, "eligible": 0, "fetched": 0, "comments": 0} for s in sources}

    f, writer = open_csv_writer(out_path, RECORD_FIELDS)
    audit_file, audit_writer = open_csv_writer(os.path.join(args.output_dir, AUDIT_FILE), AUDIT_FIELDS)
    with f, audit_file:
        for idx, source in enumerate(sources):
            if idx > 0:
                print(f"\nWait {DELAY_BETWEEN_SOURCES}s before next source...")
                time.sleep(DELAY_BETWEEN_SOURCES)
            label = source["label"]
            print(f"\n=== Source: {label} ({source['source_class']}) ===")
            try:
                post_urls = discover_posts(source)
            except CookieExpiredError as exc:
                print(f"[STOP] {exc}")
                return
            except Exception as exc:
                print(f"[SEARCH ERROR] source={label!r} error={exc!r}")
                continue
            stats[label]["discovered"] = len(post_urls)

            for post_url in post_urls:
                identity = canonical_post_identity(post_url)
                if identity in seen_posts:
                    continue
                seen_posts.add(identity)
                try:
                    metadata, verdict, topic_rule, post_context, comments = fetch_eligible_post(post_url)
                    audit = {
                        "source_url": canonical_post_url(post_url), **metadata,
                        "verdict": verdict, "reason": topic_rule,
                        "discovery_query": _source_value(source, "query"),
                        "matched_groups": ",".join(matched_groups(f"{metadata.get('post_title', '')} {metadata['post_context']}")),
                        "co_thanh_pho_khac": co_thanh_pho_khac(metadata.get("post_title", ""), metadata["post_context"]),
                        "error": "", "crawled_at": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat(),
                    }
                    audit_writer.writerow(audit)
                    if verdict != "accept":
                        print(f"[TOPIC SKIP] {post_url} rule={topic_rule}")
                        continue
                    stats[label]["eligible"] += 1
                except Exception as exc:
                    audit_writer.writerow({"source_url": canonical_post_url(post_url), "verdict": "error", "reason": "fetch_error", "discovery_query": _source_value(source, "query"), "error": repr(exc), "crawled_at": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).isoformat()})
                    print(f"[SKIP] post={post_url} first error={exc!r}; retry after 5s")
                    time.sleep(5)
                    try:
                        metadata, verdict, topic_rule, post_context, comments = fetch_eligible_post(post_url)
                        if verdict != "accept":
                            continue
                    except Exception as retry_exc:
                        print(f"[SKIP] post={post_url} retry error={retry_exc!r}")
                        continue
                stats[label]["fetched"] += 1
                for comment_data in comments:
                    comment_data.update({"post_published_at_raw": metadata.get("post_published_at_raw", ""), "post_published_at": metadata.get("post_published_at", "")})
                    record = to_record(post_url, post_context, comment_data, batch_id, source, topic_rule)
                    if record["id"] in written_ids:
                        continue
                    written_ids.add(record["id"])
                    writer.writerow(flatten_record(record))
                    stats[label]["comments"] += 1
            print(f"[SOURCE STATS] {label} {stats[label]}")

    print(f"posts crawled: {len(seen_posts)}")
    print(f"comments written: {len(written_ids)}")
    print(f"source stats: {json.dumps(stats, ensure_ascii=False)}")
    print(f"Output: {out_path}")


if __name__ == "__main__":
    main()
