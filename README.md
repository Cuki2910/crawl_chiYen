# Hanoi Flood Transport Crawler

YouTube, Facebook, TikTok, Threads, and News (VnExpress/Thanh Nien/VietnamNet)
comment crawler for Hanoi flood-related transport content.

## Scope

- Keyword source: `Danh mục từ khoá crawl dữ liệu.xlsx`, sheet `Ngập lụt`.
- Ignored: sheet `Xe bus điện`.
- Publication windows, Vietnam time:
  - `2025-08-01` through `2025-10-31`.
  - `2026-08-01` through crawl time.
- Comments are collected only from posts/videos/articles whose publication
  time is in scope.

## Two crawler families, one shared gate

The four platforms split into two independently-built pipelines that share
`crawlers/topics.py` (the gate) and `crawlers/common/records.py` (record
helpers), but nothing else -- each pipeline keeps its own discovery/search
method and orchestration style:

- **YouTube + Facebook** (`crawlers/youtube/`, `crawlers/facebook/`): a
  long-running supervisor (`crawlers/baseline_runner.py`) drives both
  platforms in parallel worker threads against a shared SQLite store
  (`crawlers/baseline_store.py`), targeting a comment-count milestone over
  a fixed time budget. Search queries come from
  `crawlers/hanoi_flood_transport/query_gen.py`'s algorithmic pairing of
  the 3 keyword groups.
- **TikTok + News** (`crawlers/tiktok/`, `crawlers/news/`): a batch
  discover-review-crawl flow -- `discover_*.py` writes a candidate CSV for
  human review (`keep` column), then `crawl_*.py --url-file <reviewed
  list>` fetches comments, writing JSONL. Search queries come from
  `crawlers/hanoi_flood_transport/tiktok_news_queries.py`'s hand-built
  100-query list (denser per-location coverage than query_gen.py's
  algorithmic pairing -- built independently before the two crawler sets
  were merged into this repo).
- **Threads** (`crawlers/threads/`): same discover-review-crawl shape as
  TikTok/News, reusing the same 100-query list. Threads gates logged-out
  browsers behind a login wall almost immediately (unlike TikTok, which
  tolerates some anonymous browsing), so it always runs against a
  persistent, manually-logged-in browser profile -- see Setup below. Its
  in-page selectors (`discover_threads.py`/`crawl_threads.py`) are
  best-effort placeholders, not yet confirmed against live Threads markup;
  verify them before an unattended run.

Both pipelines' search queries derive from the same
`crawlers/hanoi_flood_transport/keywords.py` (the 3 groups, transcribed
from the spreadsheet), and both gate through the same `crawlers/topics.py`.
Threads reuses the TikTok/News query list and gates through the same file
via `is_on_topic_threads`.

## Setup

Create `.env` locally. Never commit it:

```text
YOUTUBE_API_KEY=...
FB_HANDLE_SALT=...
TIKTOK_HANDLE_SALT=...
NEWS_HANDLE_SALT=...
THREADS_HANDLE_SALT=...
```

`TIKTOK_HANDLE_SALT`/`NEWS_HANDLE_SALT`/`THREADS_HANDLE_SALT` pseudonymize
commenter handles for those crawlers the same way `FB_HANDLE_SALT` does for
Facebook -- generate each with
`python -c "import secrets; print(secrets.token_hex(16))"`, one per
platform, never shared or committed.

Install dependencies:

```powershell
python -m pip install -r requirements.txt
patchright install chromium
```

`patchright install chromium` is needed for the TikTok and Threads
crawlers only (real browser automation); YouTube/Facebook/News don't need
it.

**Threads also needs a one-time manual login** before discovery/crawl will
work -- Threads hides search results and most reply threads from
logged-out browsers almost immediately:

```powershell
python -m crawlers.threads.threads_session
```

Log in by hand in the opened browser window; the session persists in
`data/outputs/threads/.browser_profile` (override with `--user-data-dir`)
for every later `discover_threads.py`/`crawl_threads.py` run. Re-run this
if a later run stops with `SessionExpiredError`.

## Run

**YouTube + Facebook:**

```powershell
python -m crawlers.baseline_runner run --hours 24 --target 5000 --platform all
python -m crawlers.baseline_runner status
python -m crawlers.baseline_runner export
```

Run one platform with `--platform youtube` or `--platform facebook`.

**TikTok:**

```powershell
python crawlers/tiktok/discover_tiktok.py --headful --since 2025-08-01 --until 2025-10-31 --user-data-dir data/outputs/tiktok/.browser_profile
python crawlers/tiktok/crawl_tiktok.py --url-file <reviewed url list> --headful --user-data-dir data/outputs/tiktok/.browser_profile --max-videos <count>
```

**News:**

```powershell
python crawlers/news/discover_news.py --since 2025-08-01 --until 2025-10-31
python crawlers/news/crawl_news.py --url-file <reviewed url list>
```

**Threads:**

```powershell
python crawlers/threads/discover_threads.py --headful --since 2025-08-01 --until 2025-10-31 --user-data-dir data/outputs/threads/.browser_profile
python crawlers/threads/crawl_threads.py --url-file <reviewed url list> --headful --user-data-dir data/outputs/threads/.browser_profile --max-posts <count>
```

Requires the one-time login from Setup above. Threads discovery CSVs use
`url` as the column name, same as TikTok.

TikTok, News, and Threads discovery all write a candidate CSV with a `keep` column
(`1`/`0`, pre-gated) -- review/override it, export the `keep=1` rows' URL
column to a plain list, then pass that to `crawl_*.py --url-file`. ⚠️ **URL
extraction currently has no committed script** for any of them -- every
batch in this repo so far was extracted with an ad-hoc one-off snippet, not
a rerunnable tool. A minimal example:

```powershell
python -c "import csv; rows = csv.DictReader(open('<discovery csv>', encoding='utf-8')); print('\n'.join(r['url'] for r in rows if r['keep'] == '1'))" > <output file>
```

(News discovery CSVs use `article_url` instead of `url` as the column name.)

Run discovery twice per platform, once per publication window -- **note for
News specifically**: none of the three outlets' search engines accept a
date-range query parameter, so running `discover_news.py` twice with
different `--since`/`--until` returns the same candidate set both times,
only the `in_date_window`/`keep` labeling differs. Getting more results
from a specific historical window means raising `--max-per-keyword`, not
re-running with different dates.

## Verification

```powershell
pytest -q
python -m unittest discover -s tests
```

`pytest -q` runs everything, including the YouTube/Facebook tests (some
require live API credentials in `.env`). `python -m unittest discover -s
tests` runs the plain-`unittest` subset (currently: `test_baseline.py`,
`test_regate_dataset.py`, `test_topic_config.py`,
`test_tiktok_news_crawler.py`, and `test_threads_crawler.py`), which needs
no credentials.
