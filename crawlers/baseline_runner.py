"""Supervisor 24 giờ cho baseline_gate_v2.

Chạy Facebook worker và YouTube worker song song (thread riêng, connection SQLite
riêng). Deadline bất biến. Mục tiêu 5.000 chỉ là milestone — không dừng job.
Facebook pause (auth/quarantine) KHÔNG dừng YouTube và ngược lại.

CLI:
    python -m crawlers.baseline_runner run --hours 24 --target 5000
    python -m crawlers.baseline_runner status
    python -m crawlers.baseline_runner export
"""
import argparse
import os
import sys
import threading
import time
from datetime import datetime, timezone

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from crawlers.baseline_store import BaselineStore, BASE_DIR
from crawlers.baseline_qc import check as qc_check
from crawlers.csv_utils import load_env

load_env()

_STOP = threading.Event()


def _notify(kind, msg):
    """In sentinel để background task/log gửi thông báo cho user. Không kèm secrets."""
    print(f"[[NOTIFY:{kind}]] {msg}", flush=True)


def _batch_id(prefix):
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}"


def _worker_loop(platform, run_snapshot, run_worker, idle_wait):
    """Vòng lặp một worker: gọi run_once, xử lý status, chờ, lặp đến deadline.

    Mỗi worker có store riêng (connection SQLite riêng, an toàn cho thread).
    """
    store = BaselineStore()
    run = store.active_run() or run_snapshot

    def deadline_check():
        return _STOP.is_set() or store.deadline_passed(run)

    while not deadline_check():
        try:
            result = run_worker(store, run, deadline_check, _batch_id(platform))
        except Exception as exc:  # worker crash không được kéo sập worker kia.
            store.log_event("worker_crash", platform, {"error": repr(exc)[:300]})
            _notify("crash", f"{platform} worker crash: {repr(exc)[:150]}")
            result = {"status": "crash"}

        status = result.get("status")
        if status == "deadline":
            break
        if status == "queue_empty":
            # Không còn task; chờ rồi kiểm tra lại (nguồn có thể có bài mới).
            _sleep_until(store, run, idle_wait)
        elif status == "quota_wait":
            _notify("quota", f"{platform} hết quota, chờ reset (Pacific midnight).")
            _sleep_until(store, run, 1800)
        elif status == "rate_limit_wait":
            delay = result.get("retry_seconds", 900)
            _notify("rate_limit", f"{platform} bị rate limit, chờ {delay // 60} phút.")
            _sleep_until(store, run, delay)
        elif status == "retry_wait":
            _sleep_until(store, run, result.get("retry_seconds", 60))
        elif status == "auth_required":
            _notify("auth", f"{platform} cần đăng nhập/CAPTCHA thủ công. Xử lý trong cửa sổ browser rồi worker tự resume.")
            _sleep_until(store, run, 120)
        elif status == "quarantined":
            _notify("contamination", f"{platform} nghi nhiễm bẩn dữ liệu tại {result.get('url')}. Đã pause, cần review thủ công.")
            _sleep_until(store, run, 600)
        else:
            _sleep_until(store, run, idle_wait)
    if platform == "facebook":
        from crawlers.facebook.facebook_worker import close_session
        close_session()
    store.kv_set(f"{platform}_status", "stopped")
    store.close()


def _sleep_until(store, run, seconds):
    """Ngủ nhưng thức dậy sớm nếu STOP/deadline."""
    end = time.time() + seconds
    while time.time() < end:
        if _STOP.is_set() or store.deadline_passed(run):
            return
        time.sleep(min(5, end - time.time()))


def run(hours, target, platform="all", resume=False):
    store = BaselineStore()
    if resume:
        run_obj = store.active_run()
        if not run_obj or store.deadline_passed(run_obj):
            print("Không có active run chưa hết deadline để resume.")
            store.close()
            return False
    else:
        run_obj = store.get_or_create_run(hours=hours, target_minimum=target)
    _STOP.clear()
    store.log_event("run_start", None, {"run_id": run_obj["run_id"], "deadline": run_obj["deadline_at"]})
    _notify("start", f"Bắt đầu run {run_obj['run_id']}; deadline {run_obj['deadline_at']}; target {target}.")

    workers = []
    if platform in ("all", "youtube"):
        from crawlers.youtube.youtube_worker import run_once as yt_run
        workers.append(("youtube", yt_run))
    if platform in ("all", "facebook"):
        from crawlers.facebook.facebook_worker import run_once as fb_run
        workers.append(("facebook", fb_run))
    threads = [
        threading.Thread(target=_worker_loop, args=(name, run_obj, worker, 300), daemon=True)
        for name, worker in workers
    ]
    for t in threads:
        t.start()

    target_hit = False
    last_export = 0.0
    final_status = "completed"
    try:
        while not store.deadline_passed(run_obj):
            now = time.time()
            if now - last_export >= 3600:  # export + progress mỗi giờ.
                store.export_csv()
                progress = store.write_progress(run_obj)
                qc = qc_check(store)
                store.kv_set("last_qc", qc)
                if not qc["ok"]:
                    store.log_event("qc_critical", None, {"issues": qc["issues"]})
                    _notify("qc", f"QC critical: {qc['issues']}. Đã yêu cầu dừng graceful.")
                    final_status = "qc_failed"
                    _STOP.set()
                _notify("progress",
                        f"{progress['elapsed_hours']}h; total={progress['total_unique_valid']}; "
                        f"FB tl/rep={progress['facebook_top_level']}/{progress['facebook_replies']}; "
                        f"YT tl/rep={progress['youtube_top_level']}/{progress['youtube_replies']}; "
                        f"FB={progress['facebook_status']} YT={progress['youtube_status']}")
                last_export = now
            if not target_hit and store.comment_count() >= target:
                store.mark_target_reached(run_obj["run_id"])
                run_obj = store.active_run()
                target_hit = True
                _notify("target", f"Đạt {target} comments. Tiếp tục crawl đến hết deadline.")
            if not any(t.is_alive() for t in threads):
                if final_status == "completed":
                    final_status = "failed"  # cả 2 worker dừng trước deadline, không phải QC/Ctrl+C.
                break
            time.sleep(30)
    except KeyboardInterrupt:
        _notify("shutdown", "Ctrl+C: dừng graceful, đang export.")
        final_status = "interrupted"
    finally:
        _STOP.set()
        for t in threads:
            t.join(timeout=120)
        store.export_csv()
        progress = store.write_progress(run_obj)
        store.finish_run(run_obj["run_id"], final_status)
        _write_final_report(store, run_obj, progress)
        store.log_event("run_end", None, progress)
        _notify("done", f"Kết thúc ({final_status}). total={progress['total_unique_valid']}, target={target}.")
        store.close()
    return final_status == "completed"


def _write_final_report(store, run, progress):
    path = os.path.join(BASE_DIR, "final_report.md")
    lines = [
        f"# Baseline gate v2 — final report",
        "",
        f"- Run: `{run['run_id']}`",
        f"- Started: {run['started_at']}",
        f"- Deadline: {run['deadline_at']}",
        f"- Target minimum: {run['target_minimum']}",
        f"- Target reached at: {run.get('target_reached_at') or 'chưa đạt'}",
        "",
        "## Tổng hợp",
        "",
        "| Chỉ số | Giá trị |",
        "|---|---:|",
        f"| Total unique valid | {progress['total_unique_valid']} |",
        f"| Facebook top-level | {progress['facebook_top_level']} |",
        f"| Facebook replies | {progress['facebook_replies']} |",
        f"| YouTube top-level | {progress['youtube_top_level']} |",
        f"| YouTube replies | {progress['youtube_replies']} |",
        f"| Facebook accepted posts | {progress['facebook_accepted']} |",
        f"| Facebook rejected posts | {progress['facebook_rejected']} |",
        f"| YouTube accepted videos | {progress['youtube_accepted']} |",
        f"| YouTube rejected videos | {progress['youtube_rejected']} |",
        "",
        "> [!IMPORTANT]",
        "> Dữ liệu YouTube lấy qua API phải xóa/refresh trong 30 ngày kể từ crawled_at.",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def status():
    store = BaselineStore()
    run = store.active_run()
    if not run:
        print("Không có run đang chạy.")
        # vẫn in tổng comment nếu có.
    progress = store.write_progress(run) if run else {"total_unique_valid": store.comment_count()}
    import json
    print(json.dumps(progress, ensure_ascii=False, indent=2))
    store.close()


def export():
    store = BaselineStore()
    outs = store.export_csv()
    for name, (path, n) in outs.items():
        print(f"{name}: {n} rows -> {path}")
    store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--hours", type=float, default=24)
    p_run.add_argument("--target", type=int, default=5000)
    p_run.add_argument("--platform", choices=("all", "facebook", "youtube"), default="all")
    sub.add_parser("status")
    sub.add_parser("export")
    p_resume = sub.add_parser("resume")
    p_resume.add_argument("--platform", choices=("all", "facebook", "youtube"), default="all")
    args = parser.parse_args()
    if args.cmd == "run":
        run(args.hours, args.target, args.platform)
    elif args.cmd == "resume":
        run(24, 5000, args.platform, resume=True)
    elif args.cmd == "status":
        status()
    elif args.cmd == "export":
        export()
