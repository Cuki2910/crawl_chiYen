# Facebook crawler

Collects comments from public Facebook posts and Watch videos matching the
Hanoi flood-transport topic.

Sources are generated from `crawlers/hanoi_flood_transport/keywords.py`:
flood condition + transport impact + Hanoi location. The post gate runs on
metadata before comment capture. Publication dates outside the configured
windows are rejected before comments are fetched.

Run through the shared supervisor:

```powershell
python -m crawlers.baseline_runner run --hours 24 --target 5000 --platform facebook
```

Facebook requires a logged-in browser session and `FB_HANDLE_SALT`.
