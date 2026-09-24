# YouTube crawler

Uses YouTube Data API v3 to search topic queries, fetch video descriptions,
then collect top-level comments and replies.

Queries come from `crawlers/hanoi_flood_transport/keywords.py`. Search windows
are limited to `2025-08` through `2025-10` and `2026-08` through crawl time.

Run through the shared supervisor:

```powershell
python -m crawlers.baseline_runner run --hours 24 --target 5000 --platform youtube
```

Set `YOUTUBE_API_KEY` locally. YouTube API data follows the existing 30-day
retention constraint documented by the project.
