"""G05/T04: gioi han bo nho, gioi han body, hieu nang dashboard.

Khong cham du lieu that, khong dung cong 8793: benchmark chay trong TIEN TRINH CON
voi KID_TE_ROOT tro vao tmp_path; server test dung cong tu do.
Chay day du (100k interactions / 100 video): KIDTE_SLOW=1.
"""
import json
import os
import socket
import subprocess
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
SRC = BACKEND / "src"
sys.path.insert(0, str(SRC))

SLOW = os.environ.get("KIDTE_SLOW") == "1"


def _run_bench(tmp_path, interactions, videos, runs, only="/api/dashboard"):
    env = {**os.environ, "KID_TE_ROOT": str(tmp_path / "bench-root")}
    env.pop("LIVE_RE_ROOT", None)
    proc = subprocess.run(
        [sys.executable, str(BACKEND / "tools" / "bench_dashboard.py"),
         "--interactions", str(interactions), "--videos", str(videos),
         "--runs", str(runs), "--only", only, "--json"],
        capture_output=True, text=True, env=env, timeout=1800)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("JSON ")), None)
    assert line, f"bench khong ra ket qua:\n{proc.stdout}\n{proc.stderr}"
    return proc.returncode, json.loads(line[5:]), proc.stdout


def test_dashboard_small_dataset_exercises_code_path(tmp_path):
    """Luon chay: bo du lieu nho, chi kiem tra duong di + ket qua hop le."""
    _code, results, out = _run_bench(tmp_path, 1500, 5, 3)
    row = results["/api/dashboard"]
    assert 0 < row["p50"] <= row["p95"] <= row["max"], out
    assert row["p95"] < 5.0, out  # tran rong: chi bat loi hoi quy lon


@pytest.mark.slow
@pytest.mark.skipif(not SLOW, reason="set KIDTE_SLOW=1 de chay 100k interactions / 100 video")
def test_dashboard_p95_under_1s(tmp_path):
    code, results, out = _run_bench(tmp_path, 100_000, 100, 50)
    p95 = results["/api/dashboard"]["p95"]
    assert p95 < 1.0, f"/api/dashboard p95={p95 * 1000:.0f} ms (>=1000 ms)\n{out}"
    assert code == 0, out


# ----------------------------------------------------------------- cache bounded
def _engine_stub():
    import server
    engine = server.ReplyEngine.__new__(server.ReplyEngine)
    engine._lock = threading.Lock()
    engine.seen = OrderedDict()
    return server, engine


def test_cache_bounded():
    server, engine = _engine_stub()

    # 1) cache chong trung binh luan: tran 3000
    for i in range(10_000):
        engine._duplicate(f"comment-{i}")
    assert len(engine.seen) <= 3000

    # 2) map tra loi dang cho + chong echo: moi cai co vong `while len(...) > N` trong nguon
    text = (SRC / "server.py").read_text(encoding="utf-8")
    for name in ("_pending_reply_bodies", "_recent_reply_bodies"):
        assert f"while len(self.{name}) >" in text, f"{name} khong co tran kich thuoc"

    # 3) cache hash media: OrderedDict, tran 128
    assert "while len(cache) > 128" in text

    # 4) lich su tin nhan ngan (deque co maxlen)
    assert "deque(maxlen=" in text


def test_customer_history_cache_bounded(tmp_path):
    from customer_history import CustomerHistory
    stream = tmp_path / "raw_stream"
    stream.mkdir()
    with (stream / "stream_x.jsonl").open("w", encoding="utf-8") as out:
        for i in range(20_000):
            out.write(json.dumps({"ts": "2026-10-01T10:00:00+07:00", "msg_id": f"m{i}",
                                  "user_id": f"u{i}", "unique_id": f"user{i}", "nickname": f"K{i}",
                                  "text": "gia bao nhieu", "is_host": False}) + "\n")
    history = CustomerHistory(stream)
    history._refresh()
    import customer_history as ch
    assert len(history._by_customer) <= ch.MAX_CUSTOMERS, len(history._by_customer)
    assert len(history._by_customer) == ch.MAX_CUSTOMERS  # LRU giu khach moi nhat
    assert "tiktok:id:u19999" in history._by_customer


def test_customer_history_per_customer_and_host_bounded(tmp_path):
    import customer_history as ch
    stream = tmp_path / "raw_stream"
    stream.mkdir()
    with (stream / "stream_y.jsonl").open("w", encoding="utf-8") as out:
        for i in range(500):
            out.write(json.dumps({"ts": "2026-10-01T10:%02d:00+07:00" % (i % 60), "msg_id": f"m{i}",
                                  "room_id": "r", "user_id": "u1", "unique_id": "user1",
                                  "text": f"cau {i}", "is_host": False}) + chr(10))
            out.write(json.dumps({"ts": "2026-10-01T10:00:00+07:00", "is_host": True, "room_id": "r",
                                  "reply_to_username": "user1", "text": f"tra loi {i}"}) + chr(10))
    history = ch.CustomerHistory(stream)
    history._refresh()
    rows = history._by_customer["tiktok:id:u1"]
    assert len(rows) == ch.MAX_ROWS_PER_CUSTOMER
    assert rows[-1]["msg_id"] == "m499"   # giu dong MOI nhat
    assert len(history._host_replies["user1"]) == ch.MAX_HOST_REPLIES_PER_USER
    ctx = history.for_customer("tiktok:id:u1", current_room="r", current_msg_id="m499")
    assert ctx["phien_nay"]


# ----------------------------------------------------------------- body limit
def _free_port_server():
    import server
    server.Handler.engine = SimpleNamespace()  # moi route test phai bi chan truoc khi cham engine
    httpd = server.LoopbackHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_body_limit_413():
    """POST khai bao Content-Length vuot tran (8 MB) phai bi tu choi, khong doc/xu ly.

    Server hien tra 400 invalid_body_size (khong phai 413) - xem bao cao; test chap nhan
    ca hai de khoa hanh vi 'tu choi truoc khi doc body' va se dung neu doi sang 413.
    """
    import server
    previous = server.Handler.engine
    httpd = _free_port_server()
    try:
        port = httpd.server_port
        assert port != 8793
        with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
            conn.sendall((f"POST /api/focus HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                          "Content-Type: application/json\r\nContent-Length: 9000000\r\n\r\n{}").encode())
            status_line = conn.recv(300).split(b"\r\n")[0].decode("latin-1")
        status = int(status_line.split()[1])
        assert status in (400, 413), status_line
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.Handler.engine = previous
