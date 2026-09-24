# Hanoi Flood Transport Crawler

YouTube and Facebook comment crawler for Hanoi flood-related transport content.

## Scope

- Keyword source: `Danh mục từ khoá crawl dữ liệu.xlsx`, sheet `Ngập lụt`.
- Ignored: sheet `Xe bus điện`.
- Publication windows, Vietnam time:
  - `2025-08-01` through `2025-10-31`.
  - `2026-08-01` through crawl time.
- Comments are collected only from posts/videos whose publication time is in scope.

## Setup

Create `.env` locally. Never commit it:

```text
YOUTUBE_API_KEY=...
FB_HANDLE_SALT=...
```

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Run

```powershell
python -m crawlers.baseline_runner run --hours 24 --target 5000 --platform all
python -m crawlers.baseline_runner status
python -m crawlers.baseline_runner export
```

Run one platform with `--platform youtube` or `--platform facebook`.

## Verification

```powershell
pytest -q
```
