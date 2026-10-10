"""Theo dõi hàng chờ/lượt trả lời của bản live (chỉ đọc), ghi CSV mỗi 2 phút."""
import csv
import sqlite3
import sys
import time
from pathlib import Path

DB = sys.argv[1] if len(sys.argv) > 1 else "D:/KidTE/src/backend/data/customer_memory.sqlite3"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "D:/KidTE/queue-watch.csv")
EVERY = int(sys.argv[3]) if len(sys.argv) > 3 else 120


def sample(since):
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    try:
        queue = db.execute("SELECT platform,state,COUNT(*) FROM reply_comment_queue WHERE received_at>? GROUP BY 1,2",
                           (since,)).fetchall()
        turns = db.execute("SELECT platform,state,COALESCE(cancel_reason,''),COALESCE(recompose_count,0),COUNT(*) "
                           "FROM reply_turns WHERE created_at>? GROUP BY 1,2,3,4", (since,)).fetchall()
        msgs = db.execute("SELECT t.platform,m.state,COUNT(*) FROM reply_messages m JOIN reply_turns t USING(turn_id) "
                          "WHERE t.created_at>? GROUP BY 1,2", (since,)).fetchall()
    finally:
        db.close()
    return queue, turns, msgs


def main():
    start = time.time()
    new = not OUT.exists()
    with OUT.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "kind", "platform", "state", "extra", "count"])
        while True:
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            try:
                queue, turns, msgs = sample(start)
                for p, s, n in queue:
                    w.writerow([now, "queue", p, s, "", n])
                for p, s, reason, rc, n in turns:
                    w.writerow([now, "turn", p, s, f"{reason}|recompose={rc}", n])
                for p, s, n in msgs:
                    w.writerow([now, "message", p, s, "", n])
                w.writerow([now, "tick", "", "", "", 0])
            except sqlite3.Error as error:
                w.writerow([now, "error", "", "", str(error)[:120], 0])
            f.flush()
            time.sleep(EVERY)


if __name__ == "__main__":
    main()
