"""SQLite store cho chiến dịch baseline_gate_v2.

Nguồn chuẩn (source of truth) khi crawl. CSV chỉ là export định kỳ. Dùng WAL để
Facebook worker và YouTube worker (2 connection riêng) ghi song song an toàn.
Deadline của run là bất biến: resume không được đẩy deadline ra xa.

Chỉ dùng stdlib sqlite3 — không thêm dependency.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

BASE_DIR = "data/outputs/baseline_gate_v2"
DB_FILE = "crawl.db"

# Cột export CSV cho comments (Facebook + YouTube dùng chung).
COMMENT_EXPORT_FIELDS = [
    "id", "platform", "source_url", "context_id", "post_context", "post_title",
    "post_published_at_raw", "post_published_at", "comment_text", "comment_type",
    "parent_comment_id", "thread_id", "reply_depth", "posted_at_raw", "posted_at",
    "likes_count", "author_key", "matched_groups", "co_thanh_pho_khac",
    "parent_unresolved", "crawl_batch_id", "run_id", "crawled_at",
]
CONTEXT_EXPORT_FIELDS = [
    "platform", "context_id", "source_url", "post_title", "post_context",
    "post_published_at_raw", "post_published_at", "verdict", "reason",
    "metadata_resolved", "matched_groups", "co_thanh_pho_khac",
    "discovery_source", "discovery_query", "expected_comment_count",
    "captured_comment_count", "missing_comment_count", "comment_capture_status",
    "comment_retry_count", "crawled_at",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    target_minimum INTEGER NOT NULL,
    status TEXT NOT NULL,
    target_reached_at TEXT
);
CREATE TABLE IF NOT EXISTS contexts (
    platform TEXT NOT NULL,
    context_id TEXT NOT NULL,
    source_url TEXT,
    post_title TEXT,
    post_context TEXT,
    post_published_at_raw TEXT,
    post_published_at TEXT,
    verdict TEXT,
    reason TEXT,
    metadata_resolved INTEGER DEFAULT 0,
    matched_groups TEXT,
    co_thanh_pho_khac INTEGER DEFAULT 0,
    discovery_source TEXT,
    discovery_query TEXT,
    expected_comment_count INTEGER,
    captured_comment_count INTEGER DEFAULT 0,
    missing_comment_count INTEGER,
    comment_capture_status TEXT,
    comment_retry_count INTEGER DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'discovered',
    crawled_at TEXT,
    PRIMARY KEY (platform, context_id)
);
CREATE TABLE IF NOT EXISTS comments (
    platform TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    context_id TEXT NOT NULL,
    id TEXT,
    source_url TEXT,
    post_context TEXT,
    post_title TEXT,
    post_published_at_raw TEXT,
    post_published_at TEXT,
    comment_text TEXT NOT NULL,
    comment_type TEXT NOT NULL,
    parent_comment_id TEXT,
    thread_id TEXT,
    reply_depth INTEGER DEFAULT 0,
    posted_at_raw TEXT,
    posted_at TEXT,
    likes_count INTEGER DEFAULT 0,
    author_key TEXT,
    matched_groups TEXT,
    co_thanh_pho_khac INTEGER DEFAULT 0,
    parent_unresolved INTEGER DEFAULT 0,
    crawl_batch_id TEXT,
    run_id TEXT,
    crawled_at TEXT,
    PRIMARY KEY (platform, comment_id)
);
CREATE TABLE IF NOT EXISTS discovery_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    priority INTEGER DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    lease_at REAL DEFAULT 0,
    next_retry_at REAL DEFAULT 0,
    dedup_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    platform TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_platform ON comments (platform);
CREATE INDEX IF NOT EXISTS idx_dq_claim ON discovery_queue (platform, status, priority, next_retry_at);
"""

_LEASE_SECONDS = 1800  # task đang chạy quá 30 phút coi như stale, cho phép reclaim sau crash.


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


class BaselineStore:
    """Bọc một sqlite connection. Mỗi thread/process tạo instance riêng."""

    def __init__(self, base_dir=BASE_DIR):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        self.path = os.path.join(base_dir, DB_FILE)
        self.conn = sqlite3.connect(self.path, timeout=60, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        from crawlers.facebook.crawl_facebook import parse_comment_tooltip
        self.conn.create_function(
            "facebook_comment_time_valid", 2,
            lambda raw, iso: int(bool(iso) and parse_comment_tooltip(raw or "") == iso),
            deterministic=True,
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=60000")
        self.conn.executescript(_SCHEMA)
        comment_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(comments)")
        }
        if "posted_at" not in comment_columns:
            self.conn.execute("ALTER TABLE comments ADD COLUMN posted_at TEXT")
        context_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(contexts)")
        }
        for column, definition in (
            ("expected_comment_count", "INTEGER"),
            ("captured_comment_count", "INTEGER DEFAULT 0"),
            ("missing_comment_count", "INTEGER"),
            ("comment_capture_status", "TEXT"),
            ("comment_retry_count", "INTEGER DEFAULT 0"),
        ):
            if column not in context_columns:
                self.conn.execute(f"ALTER TABLE contexts ADD COLUMN {column} {definition}")

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def _tx(self):
        """Transaction ngắn; commit/rollback tự động. Không giữ qua network call."""
        for attempt in range(6):
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError:
                if attempt == 5:
                    raise
                time.sleep(0.5)
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ---- run lifecycle ----------------------------------------------------
    def get_or_create_run(self, hours, target_minimum):
        """Tạo run mới hoặc resume run đang chạy. Deadline bất biến khi resume."""
        row = self.conn.execute(
            "SELECT * FROM runs WHERE status IN ('running','initializing') ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row:
            return dict(row)
        started = _utcnow()
        run = {
            "run_id": "baseline_" + started.strftime("%Y%m%d_%H%M%S"),
            "started_at": _iso(started),
            "deadline_at": _iso(started + timedelta(hours=hours)),
            "target_minimum": target_minimum,
            "status": "running",
            "target_reached_at": None,
        }
        with self._tx() as c:
            c.execute(
                "INSERT INTO runs (run_id, started_at, deadline_at, target_minimum, status) VALUES (?,?,?,?,?)",
                (run["run_id"], run["started_at"], run["deadline_at"], target_minimum, "running"),
            )
        return run

    def active_run(self):
        row = self.conn.execute(
            "SELECT * FROM runs WHERE status IN ('running','initializing') ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def deadline_passed(self, run):
        return _utcnow() >= datetime.fromisoformat(run["deadline_at"])

    def mark_target_reached(self, run_id):
        with self._tx() as c:
            c.execute(
                "UPDATE runs SET target_reached_at=? WHERE run_id=? AND target_reached_at IS NULL",
                (_iso(_utcnow()), run_id),
            )

    def finish_run(self, run_id, status="completed"):
        with self._tx() as c:
            c.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run_id))

    # ---- discovery queue --------------------------------------------------
    def enqueue(self, platform, source, payload, priority=100, dedup_key=None):
        dedup_key = dedup_key or f"{platform}:{source}:{json.dumps(payload, sort_keys=True, ensure_ascii=False)}"
        try:
            with self._tx() as c:
                c.execute(
                    "INSERT INTO discovery_queue (platform, source, payload, priority, dedup_key) VALUES (?,?,?,?,?)",
                    (platform, source, json.dumps(payload, ensure_ascii=False), priority, dedup_key),
                )
            return True
        except sqlite3.IntegrityError:
            return False  # đã có trong queue, bỏ qua.

    def claim(self, platform):
        """Lấy 1 task pending (hoặc stale-running) theo priority. Atomic."""
        now = time.time()
        with self._tx() as c:
            row = c.execute(
                """SELECT * FROM discovery_queue
                   WHERE platform=? AND next_retry_at<=?
                     AND (status='pending' OR (status='running' AND lease_at < ?))
                   ORDER BY priority ASC, id ASC LIMIT 1""",
                (platform, now, now - _LEASE_SECONDS),
            ).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE discovery_queue SET status='running', lease_at=?, attempts=attempts+1 WHERE id=?",
                (now, row["id"]),
            )
            task = dict(row)
        task["payload"] = json.loads(task["payload"])
        return task

    def complete_task(self, task_id, status="done"):
        with self._tx() as c:
            c.execute("UPDATE discovery_queue SET status=?, lease_at=0 WHERE id=?", (status, task_id))

    def retry_task(self, task_id, delay=60, max_attempts=5):
        with self._tx() as c:
            row = c.execute("SELECT attempts FROM discovery_queue WHERE id=?", (task_id,)).fetchone()
            if row and row["attempts"] >= max_attempts:
                c.execute("UPDATE discovery_queue SET status='failed', lease_at=0 WHERE id=?", (task_id,))
            else:
                c.execute(
                    "UPDATE discovery_queue SET status='pending', lease_at=0, next_retry_at=? WHERE id=?",
                    (time.time() + delay, task_id),
                )

    def defer_task(self, task_id, delay=0):
        """Trả task về pending không áp max attempts (dùng cho quota/rate-limit toàn cục)."""
        with self._tx() as c:
            c.execute(
                "UPDATE discovery_queue SET status='pending', lease_at=0, next_retry_at=? WHERE id=?",
                (time.time() + delay, task_id),
            )

    def pending_count(self, platform):
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM discovery_queue WHERE platform=? AND status IN ('pending','running')",
            (platform,),
        ).fetchone()
        return row["n"]

    # ---- contexts ---------------------------------------------------------
    def upsert_context(self, ctx):
        """Insert/update context. Trả False (không ghi) nếu bị chặn downgrade accept->reject
        khi đã có comment — dữ liệu comment cũ chỉ hợp lệ khi context còn accept."""
        cols = ["platform", "context_id", "source_url", "post_title", "post_context",
                "post_published_at_raw", "post_published_at", "verdict", "reason",
                "metadata_resolved", "matched_groups", "co_thanh_pho_khac",
                "discovery_source", "discovery_query", "expected_comment_count",
                "captured_comment_count", "missing_comment_count", "comment_capture_status",
                "comment_retry_count", "state", "crawled_at"]
        values = [ctx.get(k) for k in cols]
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{k}=excluded.{k}" for k in cols if k not in ("platform", "context_id"))
        platform, context_id = ctx["platform"], ctx["context_id"]
        with self._tx() as c:
            if ctx.get("verdict") != "accept":
                existing = c.execute(
                    "SELECT verdict FROM contexts WHERE platform=? AND context_id=?",
                    (platform, context_id),
                ).fetchone()
                if existing and existing["verdict"] == "accept":
                    has_comments = c.execute(
                        "SELECT 1 FROM comments WHERE platform=? AND context_id=? LIMIT 1",
                        (platform, context_id),
                    ).fetchone()
                    if has_comments:
                        c.execute(
                            "INSERT INTO events (ts, kind, platform, detail) VALUES (?,?,?,?)",
                            (_iso(_utcnow()), "context_downgrade_blocked", platform,
                             json.dumps({"context_id": context_id, "incoming_verdict": ctx.get("verdict"),
                                         "incoming_reason": ctx.get("reason")}, ensure_ascii=False)),
                        )
                        return False
            c.execute(
                f"INSERT INTO contexts ({','.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(platform, context_id) DO UPDATE SET {updates}",
                values,
            )
        return True

    def context_state(self, platform, context_id):
        row = self.conn.execute(
            "SELECT state FROM contexts WHERE platform=? AND context_id=?", (platform, context_id)
        ).fetchone()
        return row["state"] if row else None

    def context(self, platform, context_id):
        row = self.conn.execute(
            "SELECT * FROM contexts WHERE platform=? AND context_id=?", (platform, context_id)
        ).fetchone()
        return dict(row) if row else None

    def facebook_contexts_needing_reconciliation(self, limit=200):
        rows = self.conn.execute(
            """SELECT * FROM contexts
               WHERE platform='facebook'
                 AND state IN ('comments_done','comments_incomplete','comments_unverified')
                 AND COALESCE(comment_retry_count, 0) < 3
               ORDER BY crawled_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def comment_count_for_context(self, platform, context_id):
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM comments WHERE platform=? AND context_id=?",
            (platform, context_id),
        ).fetchone()
        return row["n"]

    def set_context_state(self, platform, context_id, state):
        with self._tx() as c:
            c.execute(
                "UPDATE contexts SET state=? WHERE platform=? AND context_id=?",
                (state, platform, context_id),
            )

    def reset_comment_retry_count(self, platform, context_id):
        with self._tx() as c:
            c.execute(
                "UPDATE contexts SET comment_retry_count=0 WHERE platform=? AND context_id=?",
                (platform, context_id),
            )

    def bump_comment_retry_count(self, platform, context_id):
        """Tăng comment_retry_count sau 1 lần refresh không thêm được comment mới.
        Dùng để chặn video "deficit ma" (API commentCount đếm comment đã ẩn/xóa,
        không lấy được qua endpoint) bị re-queue vô hạn mỗi giờ."""
        with self._tx() as c:
            c.execute(
                "UPDATE contexts SET comment_retry_count=COALESCE(comment_retry_count,0)+1 "
                "WHERE platform=? AND context_id=?",
                (platform, context_id),
            )
            row = c.execute(
                "SELECT comment_retry_count FROM contexts WHERE platform=? AND context_id=?",
                (platform, context_id),
            ).fetchone()
        return row["comment_retry_count"] if row else 0

    # ---- comments ---------------------------------------------------------
    def add_comments(self, records):
        """Insert nhiều comment idempotent. Trả về số record MỚI thực sự thêm."""
        if not records:
            return 0
        cols = ["platform", "comment_id", "context_id", "id", "source_url",
                "post_context", "post_title", "post_published_at_raw", "post_published_at",
                "comment_text", "comment_type", "parent_comment_id", "thread_id",
                "reply_depth", "posted_at_raw", "posted_at", "likes_count", "author_key",
                "matched_groups", "co_thanh_pho_khac", "parent_unresolved",
                "crawl_batch_id", "run_id", "crawled_at"]
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(
            f"{col}=excluded.{col}" for col in cols if col not in ("platform", "comment_id")
        )
        added = 0
        with self._tx() as c:
            for rec in records:
                if not (rec.get("comment_text") or "").strip():
                    continue  # không lưu comment rỗng.
                if rec.get("platform") == "facebook":
                    from crawlers.facebook.crawl_facebook import parse_comment_tooltip
                    if parse_comment_tooltip(rec.get("posted_at_raw", "")) != rec.get("posted_at"):
                        continue
                verdict_row = c.execute(
                    "SELECT verdict FROM contexts WHERE platform=? AND context_id=?",
                    (rec.get("platform"), rec.get("context_id")),
                ).fetchone()
                if not verdict_row or verdict_row["verdict"] != "accept":
                    continue  # context thiếu hoặc chưa/không accept: không lưu comment.
                cur = c.execute(
                    f"INSERT INTO comments ({','.join(cols)}) VALUES ({placeholders}) "
                    f"ON CONFLICT(platform,comment_id) DO UPDATE SET {updates} "
                    "WHERE comments.platform='facebook' "
                    "AND facebook_comment_time_valid(comments.posted_at_raw,comments.posted_at)=0 "
                    "AND facebook_comment_time_valid(excluded.posted_at_raw,excluded.posted_at)=1",
                    [rec.get(k) for k in cols],
                )
                added += cur.rowcount
        return added

    def comment_count(self, platform=None):
        valid = "(platform!='facebook' OR facebook_comment_time_valid(posted_at_raw,posted_at)=1)"
        if platform:
            row = self.conn.execute(
                f"SELECT COUNT(*) n FROM comments WHERE platform=? AND {valid}", (platform,)
            ).fetchone()
        else:
            row = self.conn.execute(f"SELECT COUNT(*) n FROM comments WHERE {valid}").fetchone()
        return row["n"]

    def counts_by_type(self):
        rows = self.conn.execute(
            "SELECT platform, comment_type, COUNT(*) n FROM comments "
            "WHERE platform!='facebook' OR facebook_comment_time_valid(posted_at_raw,posted_at)=1 "
            "GROUP BY platform, comment_type"
        ).fetchall()
        return {(r["platform"], r["comment_type"]): r["n"] for r in rows}

    def context_stats(self):
        rows = self.conn.execute(
            "SELECT platform, verdict, COUNT(*) n FROM contexts GROUP BY platform, verdict"
        ).fetchall()
        return {(r["platform"], r["verdict"]): r["n"] for r in rows}

    # ---- kv checkpoints ---------------------------------------------------
    def kv_get(self, key, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key, value):
        with self._tx() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    # ---- events -----------------------------------------------------------
    def log_event(self, kind, platform=None, detail=None):
        with self._tx() as c:
            c.execute(
                "INSERT INTO events (ts, kind, platform, detail) VALUES (?,?,?,?)",
                (_iso(_utcnow()), kind, platform, json.dumps(detail, ensure_ascii=False) if detail else None),
            )

    # ---- export -----------------------------------------------------------
    def export_csv(self):
        """Tái xuất CSV từ SQLite. Atomic qua temp + os.replace."""
        import csv
        outputs = {}
        for platform in ("facebook", "youtube"):
            timestamp_filter = " AND facebook_comment_time_valid(posted_at_raw,posted_at)=1" if platform == "facebook" else ""
            rows = self.conn.execute(
                f"SELECT {','.join(COMMENT_EXPORT_FIELDS)} FROM comments WHERE platform=?{timestamp_filter} ORDER BY context_id, thread_id",
                (platform,),
            ).fetchall()
            path = os.path.join(self.base_dir, f"{platform}_comments.csv")
            self._atomic_csv(path, COMMENT_EXPORT_FIELDS, rows)
            outputs[f"{platform}_comments"] = (path, len(rows))

            audit = self.conn.execute(
                f"SELECT {','.join(CONTEXT_EXPORT_FIELDS)} FROM contexts WHERE platform=? ORDER BY context_id",
                (platform,),
            ).fetchall()
            apath = os.path.join(self.base_dir, f"{platform}_post_audit.csv")
            self._atomic_csv(apath, CONTEXT_EXPORT_FIELDS, audit)
            outputs[f"{platform}_audit"] = (apath, len(audit))
        return outputs

    @staticmethod
    def _atomic_csv(path, fields, rows):
        import csv
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row[k] for k in fields})
        os.replace(tmp, path)

    def write_progress(self, run):
        """Ghi progress.json atomic."""
        counts = self.counts_by_type()
        ctx = self.context_stats()
        now = _utcnow()
        deadline = datetime.fromisoformat(run["deadline_at"])
        started = datetime.fromisoformat(run["started_at"])
        progress = {
            "run_id": run["run_id"],
            "started_at": run["started_at"],
            "deadline_at": run["deadline_at"],
            "now": _iso(now),
            "elapsed_hours": round((now - started).total_seconds() / 3600, 2),
            "remaining_hours": round((deadline - now).total_seconds() / 3600, 2),
            "target_minimum": run["target_minimum"],
            "target_reached_at": run.get("target_reached_at"),
            "total_unique_valid": self.comment_count(),
            "facebook_top_level": counts.get(("facebook", "top_level"), 0),
            "facebook_replies": counts.get(("facebook", "reply"), 0),
            "youtube_top_level": counts.get(("youtube", "top_level"), 0),
            "youtube_replies": counts.get(("youtube", "reply"), 0),
            "facebook_accepted": ctx.get(("facebook", "accept"), 0),
            "facebook_rejected": ctx.get(("facebook", "reject"), 0),
            "youtube_accepted": ctx.get(("youtube", "accept"), 0),
            "youtube_rejected": ctx.get(("youtube", "reject"), 0),
            "facebook_status": self.kv_get("facebook_status", "idle"),
            "youtube_status": self.kv_get("youtube_status", "idle"),
        }
        path = os.path.join(self.base_dir, "progress.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return progress


def self_check():
    """Smoke test không cần network: schema, dedup, claim, resume, export."""
    import tempfile
    import shutil
    tmp = tempfile.mkdtemp(prefix="baseline_store_")
    try:
        store = BaselineStore(base_dir=tmp)
        run = store.get_or_create_run(hours=24, target_minimum=5000)
        run2 = store.get_or_create_run(hours=24, target_minimum=5000)
        assert run["run_id"] == run2["run_id"], "resume must keep same run"
        assert run["deadline_at"] == run2["deadline_at"], "deadline must be immutable"

        assert store.enqueue("youtube", "search", {"q": "x"}) is True
        assert store.enqueue("youtube", "search", {"q": "x"}) is False, "dedup enqueue"
        task = store.claim("youtube")
        assert task and task["payload"]["q"] == "x"
        assert store.claim("youtube") is None, "no double claim"
        store.complete_task(task["id"])

        store.upsert_context({
            "platform": "youtube", "context_id": "v1", "source_url": "u", "post_title": "t",
            "post_context": "c", "post_published_at_raw": "", "post_published_at": "",
            "verdict": "accept", "reason": "ok", "metadata_resolved": 1, "matched_groups": "A",
            "co_thanh_pho_khac": 0, "discovery_source": "s", "discovery_query": "q",
            "state": "accepted", "crawled_at": "now",
        })
        rec = {
            "platform": "youtube", "comment_id": "c1", "context_id": "v1", "id": "youtube_c_c1",
            "source_url": "u", "post_context": "c", "post_title": "t", "post_published_at_raw": "",
            "post_published_at": "", "comment_text": "hello", "comment_type": "top_level",
            "parent_comment_id": None, "thread_id": "c1", "reply_depth": 0, "posted_at_raw": "",
            "likes_count": 0, "author_key": None, "matched_groups": "", "co_thanh_pho_khac": 0,
            "parent_unresolved": 0, "crawl_batch_id": "b", "run_id": run["run_id"], "crawled_at": "now",
        }
        assert store.add_comments([rec]) == 1
        assert store.add_comments([rec]) == 0, "dedup comment"
        empty = dict(rec, comment_id="c2", comment_text="   ")
        assert store.add_comments([empty]) == 0, "empty text skipped"
        assert store.comment_count() == 1

        store.kv_set("yt_cursor", {"token": "abc"})
        assert store.kv_get("yt_cursor")["token"] == "abc"

        outs = store.export_csv()
        assert outs["youtube_comments"][1] == 1
        store.write_progress(run)
        store.close()
        print("baseline_store self-check OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    self_check()
