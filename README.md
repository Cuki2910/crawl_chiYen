# Hanoi Flood Transport Crawler

YouTube and Facebook comment crawler for Hanoi flood-related transport content.

## Scope

- Keyword sources: `Danh mục từ khoá crawl dữ liệu.xlsx`, sheets `Ngập lụt` and `Xe bus điện`.
- Electric-bus queries run separately, one keyword at a time, from `2026-08-01` to crawl time.
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
python -m crawlers.baseline_runner run --campaign flood_transport --hours 24 --target 5000 --platform all
python -m crawlers.baseline_runner run --campaign electric_bus --hours 24 --target 5000 --platform all
python -m crawlers.baseline_runner status --campaign electric_bus
python -m crawlers.baseline_runner export --campaign electric_bus
```

Run the two `run` commands in separate terminals for parallel campaigns. Each writes its own DB and CSV under `data/outputs/baseline_gate_v2/<campaign>/`.

## Verification

```powershell
pytest -q
```
