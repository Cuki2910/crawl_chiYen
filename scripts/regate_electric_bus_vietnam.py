"""One-off: drop non-Vietnam YouTube videos from the electric_bus dataset."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import regate_dataset as rd
from crawlers.topics import gate_electric_bus_vietnam

rd.OUT_DIR = Path("data/outputs/baseline_gate_v2/electric_bus")


def main():
    con = sqlite3.connect(rd.OUT_DIR / "crawl.db")
    try:
        rows = con.execute(
            "SELECT context_id, post_title, post_context FROM contexts WHERE platform='youtube' AND verdict='accept'"
        ).fetchall()
        dropped = [
            (cid, title) for cid, title, body in rows
            if gate_electric_bus_vietnam(title or "", body or "")[1] == "not_vietnam"
        ]
        for cid, _ in dropped:
            con.execute(
                "UPDATE contexts SET verdict='reject', reason='not_vietnam', state='rejected' "
                "WHERE platform='youtube' AND context_id=?", (cid,),
            )
        purged = rd.purge_nonaccepted_comments(con)
        con.commit()
        rd.export_context_csv(con, "youtube", rd.OUT_DIR / "youtube_post_audit.csv")
        rd.export_comment_csv(con, "youtube", rd.OUT_DIR / "youtube_comments.csv")
        rd.update_progress(con)
        print(f"youtube accepted before: {len(rows)}, dropped not_vietnam: {len(dropped)}, comments purged: {purged}")
        for cid, title in dropped[:15]:
            print(f"  DROP {cid}: {title[:70]}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
