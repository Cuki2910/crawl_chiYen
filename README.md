# Hanoi Flood Transport Crawler

YouTube, Facebook, TikTok, and News (VnExpress/Thanh Nien/VietnamNet)
comment crawler for Hanoi flood-related transport content.

## Scope

- Keyword sources: `Danh mục từ khoá crawl dữ liệu.xlsx`, sheets `Ngập lụt` and `Xe bus điện`.
- Electric-bus queries run separately, one keyword at a time, from `2026-08-01` to crawl time.
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

Both pipelines' search queries derive from the same
`crawlers/hanoi_flood_transport/keywords.py` (the 3 groups, transcribed
from the spreadsheet), and both gate through the same `crawlers/topics.py`.

### Electric-bus campaign (TikTok + News)

All four `crawlers/tiktok/discover_tiktok.py`, `crawlers/tiktok/crawl_tiktok.py`,
`crawlers/news/discover_news.py`, `crawlers/news/crawl_news.py` scripts take
a `--campaign {flood_transport,electric_bus}` flag (default
`flood_transport`, so every existing flood command still works unchanged).
`electric_bus` switches three things:
- **Search queries** -> `crawlers/hanoi_flood_transport/electric_bus_queries.py`
  (47 queries: the 30 `ELECTRIC_BUS` keyword-group terms deduped down to
  26 unique, plus 20 broader/rephrased queries for recall). Search-side
  only -- widening these queries does not widen the gate.
- **Gate** -> `gate_electric_bus()` (`crawlers/topics.py`), a single-group
  match against the original `ELECTRIC_BUS` term list in `keywords.py` --
  accept if *any* electric-bus term is present, unlike `gate_post()`'s
  3-group AND requirement for flood-transport. A query above can surface a
  candidate whose text never contains one of the exact gate terms; that
  candidate is still rejected.
- **Output batch prefix** -> `tiktok_electric_bus_*` /
  `news_electric_bus_*` instead of `tiktok_flood_*` / `news_flood_*`, so
  the two topics' output files are never ambiguous (see Output locations
  below).

There is only one publication window for this campaign -- `2026-08-01`
through crawl time -- so `--since`/`--until` only needs to be passed once,
not run twice per platform like the flood-transport windows.

## Setup

Create `.env` locally. Never commit it:

```text
YOUTUBE_API_KEY=...
FB_HANDLE_SALT=...
TIKTOK_HANDLE_SALT=...
NEWS_HANDLE_SALT=...
```

`TIKTOK_HANDLE_SALT`/`NEWS_HANDLE_SALT` pseudonymize commenter handles for
those two crawlers the same way `FB_HANDLE_SALT` does for Facebook --
generate each with `python -c "import secrets; print(secrets.token_hex(16))"`,
one per platform, never shared or committed.

Install dependencies:

```powershell
python -m pip install -r requirements.txt
patchright install chromium
```

`patchright install chromium` is needed for the TikTok crawler only (real
browser automation); YouTube/Facebook/News don't need it.

## Run

**YouTube + Facebook:**

```powershell
python -m crawlers.baseline_runner run --campaign flood_transport --hours 24 --target 5000 --platform all
python -m crawlers.baseline_runner run --campaign electric_bus --hours 24 --target 5000 --platform all
python -m crawlers.baseline_runner status --campaign electric_bus
python -m crawlers.baseline_runner export --campaign electric_bus
```

Run the two `run` commands in separate terminals for parallel campaigns. Each writes its own DB and CSV under `data/outputs/baseline_gate_v2/<campaign>/`.

**TikTok:**

```powershell
python crawlers/tiktok/discover_tiktok.py --campaign flood_transport --headful --since 2025-08-01 --until 2025-10-31 --user-data-dir data/outputs/tiktok/.browser_profile
python crawlers/tiktok/crawl_tiktok.py --campaign flood_transport --url-file <reviewed url list> --headful --user-data-dir data/outputs/tiktok/.browser_profile --max-videos <count>

python crawlers/tiktok/discover_tiktok.py --campaign electric_bus --headful --since 2026-08-01 --user-data-dir data/outputs/tiktok/.browser_profile
python crawlers/tiktok/crawl_tiktok.py --campaign electric_bus --url-file <reviewed url list> --headful --user-data-dir data/outputs/tiktok/.browser_profile --max-videos <count>
```

**News:**

```powershell
python crawlers/news/discover_news.py --campaign flood_transport --since 2025-08-01 --until 2025-10-31
python crawlers/news/crawl_news.py --campaign flood_transport --url-file <reviewed url list>

python crawlers/news/discover_news.py --campaign electric_bus --since 2026-08-01
python crawlers/news/crawl_news.py --campaign electric_bus --url-file <reviewed url list>
```

`--campaign` defaults to `flood_transport`, so it can be omitted for every
existing flood command -- shown explicitly above for clarity.

Both TikTok and News discovery write a candidate CSV with a `keep` column
(`1`/`0`, pre-gated) -- review/override it, export the `keep=1` rows' URL
column to a plain list, then pass that to `crawl_*.py --url-file`. ⚠️ **URL
extraction currently has no committed script** for either platform -- every
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

## Output locations

Everything lands under `data/outputs/`, one subdirectory per platform
(`youtube/`, `facebook/`, `tiktok/`, `news/`). Almost all of it is
gitignored (see `.gitignore`) -- only a handful of specific files are
allowlisted as committed samples; everything else (browser profiles, spike
dumps, other batches) stays local-only.

**YouTube + Facebook (`baseline_runner.py`)** work differently from
TikTok/News -- there's no per-run discovery/crawl file pair. Instead:
- `data/outputs/baseline_gate_v2/crawl.db` -- the single running SQLite
  store both platforms write into continuously while `baseline_runner run`
  is active.
- `progress.json` -- current run's live counters (`baseline_runner status`
  regenerates it).
- `final_report.md` -- written when a run finishes or is exported
  (`baseline_runner export`), with the same counters as a readable summary.
- `facebook_comments.csv` / `youtube_comments.csv` and the matching
  `*_post_audit.csv` -- the actual exported rows, written by
  `baseline_runner export`.
- `data/outputs/facebook_smoke/<YYYYMMDD_HHMMSS>/` -- separate, timestamped
  one-off smoke-test runs (`facebook_smoke.py`), not part of the main
  pipeline's output; safe to ignore/delete.

**TikTok and News (`discover_*.py` / `crawl_*.py`)** each run writes one
timestamped file:
- Discovery -> a CSV: `tiktok_flood_discovery_<batch>.csv` (TikTok) or
  `discovery_news_flood_<batch>.csv` (News). One row per candidate
  post/article, with the gate's `gate_verdict`/`gate_rule` and a `keep`
  column for human review.
- Crawl -> a JSONL: `tiktok_flood_<batch>.jsonl` or `news_flood_<batch>.jsonl`.
  One line per comment/reply record.
- With `--campaign electric_bus`, the prefix swaps to
  `tiktok_electric_bus_*` / `news_electric_bus_*` (discovery CSVs) and the
  matching `*.jsonl` crawl output -- distinguishing the two topics' files
  is automatic here, unlike the publication-window ambiguity described
  below (which still applies within the flood-transport campaign only;
  electric-bus has a single window, so there's nothing to disambiguate
  there).

**`<batch>` is `YYYYMMDD_HHMM` in Asia/Ho_Chi_Minh time -- it's when that
run happened, not which publication window it targeted.** The two
publication windows (`2025-08-01`..`2025-10-31` and `2026-08-01`..now) are
passed as `--since`/`--until` at run time but are **not** recorded in the
output filename. Only rows inside the requested window are ever written
(`in_date_window` filters before the CSV write), so within one platform,
batches run in the order below -- earlier batch = first window, later
batch = second window:

| Platform | Order | Publication window | Discovery CSV | Crawl JSONL |
|---|---|---|---|---|
| TikTok | 1st | `2025-08-01` .. `2025-10-31` | `tiktok_flood_discovery_20260924_2120.csv` | `tiktok_flood_20260924_2307.jsonl` |
| TikTok | 2nd | `2026-08-01` .. now | `tiktok_flood_discovery_20260924_2241.csv` | `tiktok_flood_20260925_0900.jsonl` |
| News | 1st | `2025-08-01` .. `2025-10-31` | `discovery_news_flood_20260924_2302.csv` | `news_flood_20260925_1411.jsonl` |
| News | 2nd | `2026-08-01` .. now | `discovery_news_flood_20260924_2304.csv` | `news_flood_20260925_1734.jsonl` |

Nothing in the repo enforces or records this mapping automatically --
future runs must be tracked the same way (note which `--since`/`--until`
pair produced which batch file), or cross-checked against the file's own
date column (`create_time` for TikTok, the article date column for News).

## Verification

```powershell
pytest -q
python -m unittest discover -s tests
```

`pytest -q` runs everything, including the YouTube/Facebook tests (some
require live API credentials in `.env`). `python -m unittest discover -s
tests` runs the plain-`unittest` subset (currently: `test_baseline.py`,
`test_regate_dataset.py`, `test_topic_config.py`,
`test_tiktok_news_crawler.py`, and `test_electric_bus_campaign.py`), which
needs no credentials.
