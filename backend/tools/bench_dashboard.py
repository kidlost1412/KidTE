"""Benchmark /api/dashboard va cac danh sach chinh tren du lieu lon (G05/T04).

Sinh N interactions + M video vao mot THU MUC TAM (khong cham du lieu that,
khong dung cong 8793), chay ReplyEngine + Handler trong tien trinh tren cong
trong, do moi endpoint `--runs` lan, in p50/p95. Thoat != 0 neu p95 >= nguong.

    ..\\.venv\\Scripts\\python.exe tools\\bench_dashboard.py
    ..\\.venv\\Scripts\\python.exe tools\\bench_dashboard.py --interactions 5000 --videos 10 --runs 5
"""
from __future__ import annotations

import argparse
import atexit
import shutil
import json
import os
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
ENDPOINTS = (
    "/api/dashboard",
    "/api/workspace/interactions?limit=50&view=summary",
    "/api/workspace/conversations?limit=50&view=summary",
    "/api/workspace/customers?limit=50&view=summary",
    "/api/workspace/sessions?limit=50&view=summary",
    "/api/workspace/videos?limit=50&view=summary",
)


def _is_under_tmp(root: Path) -> bool:
    tmp = Path(tempfile.gettempdir()).resolve()
    try:
        root.resolve().relative_to(tmp)
        return True
    except ValueError:
        return False


def generate_data(root: Path, interactions: int, videos: int) -> None:
    """Chen truc tiep vao schema that: SQLite interactions + raw_stream + video."""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    now = time.time()
    customers = max(50, interactions // 20)
    from customer_memory import CustomerMemory
    mem = CustomerMemory(data / "customer_memory.sqlite3", root / "logs" / "journal",
                         channel="bench", session_id="bench_seed")
    mem.close()  # chi de tao schema
    db = sqlite3.connect(data / "customer_memory.sqlite3")
    try:
        def iso(t):
            return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t))
        sessions = max(1, interactions // 500)
        with db:
            db.executemany(
                "INSERT OR IGNORE INTO sessions(session_id,channel,room_id,started_at) VALUES(?,?,?,?)",
                [(f"bench_{i}", "bench", f"room{i}", iso(now - i * 3600)) for i in range(sessions)])
            db.executemany(
                "INSERT OR IGNORE INTO customers(customer_key,platform_user_id,unique_id,display_name,"
                "identity_strength,first_seen,last_seen,last_session_id) VALUES(?,?,?,?,?,?,?,?)",
                [(f"tiktok:u{i}", f"u{i}", f"user{i}", f"Khach {i}", 3, iso(now - 86400),
                  iso(now), "bench_0") for i in range(customers)])
            db.executemany(
                "INSERT INTO interactions(interaction_id,session_id,happened_at,customer_key,event_type,"
                "comment,response,response_complete,delivery_status,product_id,product_title,metadata_json,"
                "runtime_comment_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ((uuid.uuid4().hex, f"bench_{i % sessions}", iso(now - i), f"tiktok:u{i % customers}",
                  "comment", f"gia san pham {i % 40} bao nhieu shop", "Da, gia 120.000d a",
                  1, "confirmed", f"SP-{i % 40:02d}", f"San pham {i % 40}",
                  json.dumps({"room_id": f"room{i % sessions}"}), f"c{i}")
                 for i in range(interactions)))
    finally:
        db.close()
    # raw_stream: nguon chinh cua tong quan / hoi thoai
    raw_dir = root / "logs" / "raw_stream"
    raw_dir.mkdir(parents=True, exist_ok=True)
    per_file = 25_000
    for part in range((interactions + per_file - 1) // per_file):
        with (raw_dir / f"stream_bench_{part:03d}.jsonl").open("w", encoding="utf-8") as out:
            for i in range(part * per_file, min(interactions, (part + 1) * per_file)):
                out.write(json.dumps({
                    "ts": iso(now - interactions + i), "msg_id": f"m{i}", "room_id": f"room{i % 20}",
                    "user_id": f"u{i % customers}", "unique_id": f"user{i % customers}",
                    "nickname": f"Khach {i % customers}", "text": f"gia san pham {i % 40} bao nhieu shop",
                    "is_host": False, "data_source": "tiktok-live"}) + "\n")
    # video
    for v in range(videos):
        vid = f"bench_video_{v:03d}"
        folder = data / "videos" / vid
        folder.mkdir(parents=True, exist_ok=True)
        timeline = {"video_id": vid, "title": f"Video {v}", "cycle_ms": 600000,
                    "source_file": f"video{v}.mp4",
                    "products": {f"SP-{k:02d}": {"bag_index": k, "include_all": [f"sp{k}"]} for k in range(5)},
                    "events": [{"at_ms": k * 10000, "product": f"SP-{k % 5:02d}"} for k in range(60)]}
        (folder / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
        (folder / "fingerprint.npz").write_bytes(os.urandom(256))
        (folder / "transcript.json").write_text(
            json.dumps({"segments": [{"start": k, "text": f"doan {k}"} for k in range(50)]}),
            encoding="utf-8")


class _NoModel:
    """Responder gia: khong bao gio goi mang."""

    model = "bench-fake"

    def generate(self, *args, **kwargs):
        raise RuntimeError("bench does not use the model")


def start_server():
    """ReplyEngine + Handler tren cong trong 127.0.0.1. Tra (server, engine, port)."""
    import server as srv
    engine = srv.ReplyEngine(responder=_NoModel())
    srv.Handler.engine = engine
    httpd = srv.LoopbackHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, engine, httpd.server_port


def stop_server(httpd, engine):
    httpd.shutdown()
    httpd.server_close()
    try:
        engine.close()
    except Exception:
        pass


def percentile(values, pct):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = max(1, -(-len(ordered) * pct // 100))  # nearest-rank
    return ordered[int(rank) - 1]


def time_endpoint(port, path, runs):
    url = f"http://127.0.0.1:{port}{path}"
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        with urllib.request.urlopen(url, timeout=120) as response:
            response.read()
            if response.status != 200:
                raise RuntimeError(f"{path} -> {response.status}")
        samples.append(time.perf_counter() - started)
    return samples


def run_bench(interactions, videos, runs, endpoints=ENDPOINTS):
    """Tra {endpoint: {p50,p95,max}} (giay). ROOT phai la thu muc tam."""
    import config
    if not _is_under_tmp(Path(config.ROOT)):
        raise SystemExit(f"ROOT {config.ROOT} khong nam trong thu muc tam; tu choi chay")
    generate_data(Path(config.ROOT), interactions, videos)
    httpd, engine, port = start_server()
    results = {}
    try:
        for path in endpoints:
            time_endpoint(port, path, 1)  # lan dau (nap nguoi) khong tinh
            samples = time_endpoint(port, path, runs)
            results[path] = {"p50": statistics.median(samples),
                             "p95": percentile(samples, 95), "max": max(samples)}
    finally:
        stop_server(httpd, engine)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interactions", type=int, default=100_000)
    parser.add_argument("--videos", type=int, default=100)
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--only", default="", help="chi do endpoint co chuoi nay (vd: dashboard)")
    parser.add_argument("--json", action="store_true", help="in them mot dong JSON ket qua")
    parser.add_argument("--threshold", type=float, default=1.0, help="giay; p95 >= nguong -> exit 1")
    args = parser.parse_args(argv)
    if "KID_TE_ROOT" not in os.environ and "LIVE_RE_ROOT" not in os.environ:
        _bench_tmp = tempfile.mkdtemp(prefix="kid-te-bench-")
        atexit.register(shutil.rmtree, _bench_tmp, ignore_errors=True)
        os.environ["KID_TE_ROOT"] = _bench_tmp
    os.environ.pop("LIVE_RE_ROOT", None)
    sys.path.insert(0, str(SRC))
    print(f"ROOT tam: {os.environ['KID_TE_ROOT']}")
    started = time.perf_counter()
    endpoints = tuple(e for e in ENDPOINTS if args.only in e)
    results = run_bench(args.interactions, args.videos, args.runs, endpoints)
    print(f"{args.interactions} interactions, {args.videos} videos, {args.runs} lan/endpoint "
          f"(tong {time.perf_counter() - started:.1f}s)")
    failed = False
    for path, row in results.items():
        bad = row["p95"] >= args.threshold
        failed |= bad
        print(f"{'FAIL' if bad else 'ok  '} p50={row['p50'] * 1000:8.1f} ms  "
              f"p95={row['p95'] * 1000:8.1f} ms  max={row['max'] * 1000:8.1f} ms  {path}")
    if args.json:
        print("JSON " + json.dumps(results))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
