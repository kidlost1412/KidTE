"""Soak do nguoi van hanh chay (T04): mac dinh 8 gio, moi phut lay mau.

Moi phut ghi vao test-artifacts/soak/soak-<ngay>.csv:
  RSS / handle / thread / so cong LISTEN cua tung tien trinh KidTE, tong so
  tien trinh, do tre API backend (/api/dashboard, /api/heartbeat khong goi - chi GET).
Tuy chon: 30 lan dieu huong trang bang Playwright toi /app (bo qua neu chua cai).
Cuoi cung ghi soak-<ngay>.md (tom tat + xu huong RSS/handle dau->cuoi).

CHI DOC: khong dung POST, khong doi trang thai. Chay tren may that khi live:

    ..\\.venv\\Scripts\\python.exe tools\\soak.py                    # 8 gio
    ..\\.venv\\Scripts\\python.exe tools\\soak.py --hours 0.05 --interval 10   # thu nhanh

Can psutil (pip install psutil). Playwright la tuy chon:
    pip install playwright ; playwright install chromium
"""
from __future__ import annotations

import argparse
import csv
import re
import statistics
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8793"
DEFAULT_MATCH = r"KidTE\.exe|KidTECaptcha|app_entry\.py|dong-bo-video"
OUT_DIR = Path(__file__).resolve().parents[2] / "test-artifacts" / "soak"
FIELDS = ["time", "elapsed_min", "proc", "pid", "rss_mb", "handles", "threads", "listeners",
          "proc_count", "api_dashboard_ms", "api_status", "nav_done"]
NAV_COUNT = 30
NAV_PATHS = ("/app", "/app#/overview", "/app#/inbox", "/app#/videos", "/app#/settings")


def kid_te_processes(psutil, pattern):
    regex = re.compile(pattern, re.IGNORECASE)
    me = psutil.Process().pid
    found = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["pid"] == me:
                continue
            text = " ".join(proc.info.get("cmdline") or []) or (proc.info.get("name") or "")
            if (regex.search(text) and "soak.py" not in text and "bench_dashboard" not in text
                    and "pytest" not in text):
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def api_latency(base_url, path="/api/dashboard"):
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(base_url + path, timeout=15) as response:
            response.read()
            status = str(response.status)
    except Exception as exc:  # backend sap cung la du lieu
        status = type(exc).__name__
    return (time.perf_counter() - started) * 1000, status


def listener_count(proc):
    try:
        conns = proc.net_connections(kind="inet") if hasattr(proc, "net_connections") \
            else proc.connections(kind="inet")
        return sum(1 for c in conns if c.status == "LISTEN")
    except Exception:
        return -1


def sample_process(proc, psutil):
    try:
        with proc.oneshot():
            rss = proc.memory_info().rss / 1048576
            threads = proc.num_threads()
            handles = proc.num_handles() if hasattr(proc, "num_handles") else proc.num_fds()
            name = proc.name()
        return name, round(rss, 1), handles, threads, listener_count(proc)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def make_navigator(base_url):
    """Tra (ham dieu_huong_mot_lan, dong). Neu khong co Playwright -> (None, ghi_chu)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "Playwright chua cai: bo qua 30 lan dieu huong (pip install playwright)"
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch()
        page = browser.new_page()
    except Exception as exc:
        return None, f"Playwright khong khoi dong duoc ({type(exc).__name__}): bo qua dieu huong"
    state = {"i": 0}

    def step():
        path = NAV_PATHS[state["i"] % len(NAV_PATHS)]
        state["i"] += 1
        page.goto(base_url + path, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(500)

    def close():
        try:
            browser.close()
            pw.stop()
        except Exception:  # noqa: BLE001 - dọn dẹp công cụ soak
            pass

    return (step, close), ""


def write_summary(path, args, rows, notes, started):
    per_proc = {}
    for row in rows:
        per_proc.setdefault(row["proc"], []).append(row)
    lat = [float(r["api_dashboard_ms"]) for r in rows if r["api_status"] == "200"]
    lines = [f"# Soak {started:%Y-%m-%d %H:%M}", "",
             f"- Thoi luong thuc: {rows[-1]['elapsed_min'] if rows else 0} phut "
             f"(yeu cau {args.hours} gio, mau moi {args.interval}s)",
             f"- Backend: {args.url}", f"- So dong mau: {len(rows)}", ""]
    if notes:
        lines += ["## Ghi chu", *[f"- {n}" for n in notes], ""]
    lines += ["## Do tre /api/dashboard", ""]
    if lat:
        ordered = sorted(lat)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        lines += [f"- p50 {statistics.median(lat):.0f} ms, p95 {p95:.0f} ms, max {max(lat):.0f} ms, "
                  f"loi {sum(1 for r in rows if r['api_status'] != '200')}/{len(rows)}"]
    else:
        lines += ["- khong co mau thanh cong"]
    lines += ["", "## Tai nguyen theo tien trinh (dau -> cuoi, lon nhat)", "",
              "| Tien trinh | RSS MB | Handle | Thread | Listener toi da |", "|---|---|---|---|---|"]
    for name, items in sorted(per_proc.items()):
        first, last = items[0], items[-1]
        lines.append(f"| {name} | {first['rss_mb']} -> {last['rss_mb']} (max {max(float(i['rss_mb']) for i in items)}) "
                     f"| {first['handles']} -> {last['handles']} (max {max(int(i['handles']) for i in items)}) "
                     f"| {first['threads']} -> {last['threads']} (max {max(int(i['threads']) for i in items)}) "
                     f"| {max(int(i['listeners']) for i in items)} |")
    counts = [int(r["proc_count"]) for r in rows]
    if counts:
        lines += ["", f"- So tien trinh KidTE: min {min(counts)}, max {max(counts)}, cuoi {counts[-1]}"]
    lines += ["", "Luu y: dung xu huong, khong dung mot mau. RSS/handle tang deu suot 8 gio la dau hieu ro ri.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hours", type=float, default=8.0)
    parser.add_argument("--interval", type=float, default=60.0, help="giay giua hai mau")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--match", default=DEFAULT_MATCH, help="regex nhan dien tien trinh KidTE")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument("--navigations", type=int, default=NAV_COUNT)
    parser.add_argument("--no-nav", action="store_true", help="khong dieu huong trinh duyet")
    args = parser.parse_args(argv)
    try:
        import psutil
    except ImportError:
        print("Can psutil: pip install psutil", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now()
    stamp = started.strftime("%Y%m%d-%H%M%S")
    csv_path, md_path = out_dir / f"soak-{stamp}.csv", out_dir / f"soak-{stamp}.md"
    notes, rows = [], []
    navigator = None
    if not args.no_nav:
        navigator, note = make_navigator(args.url)
        if note:
            notes.append(note)
            print(note)
    total_samples = max(1, int(args.hours * 3600 / args.interval))
    nav_every = max(1, total_samples // max(1, args.navigations))
    nav_done = 0
    t0 = time.monotonic()
    print(f"Soak {args.hours} gio, {total_samples} mau -> {csv_path}")
    try:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            for index in range(total_samples):
                tick = time.monotonic()
                if navigator and nav_done < args.navigations and index % nav_every == 0:
                    try:
                        navigator[0]()
                        nav_done += 1
                    except Exception as exc:
                        notes.append(f"dieu huong that bai o mau {index}: {type(exc).__name__}")
                        navigator[1]()
                        navigator = None
                latency, status = api_latency(args.url)
                procs = kid_te_processes(psutil, args.match)
                now = datetime.now().isoformat(timespec="seconds")
                elapsed = round((time.monotonic() - t0) / 60, 2)
                samples = [(p.pid, sample_process(p, psutil)) for p in procs]
                samples = [(pid, s) for pid, s in samples if s]
                base = {"time": now, "elapsed_min": elapsed, "proc_count": len(samples),
                        "api_dashboard_ms": round(latency, 1), "api_status": status, "nav_done": nav_done}
                for pid, (name, rss, handles, threads, listeners) in samples or [(0, ("none", 0, 0, 0, 0))]:
                    row = {**base, "proc": name, "pid": pid, "rss_mb": rss, "handles": handles,
                           "threads": threads, "listeners": listeners}
                    writer.writerow(row)
                    rows.append(row)
                handle.flush()
                time.sleep(max(0.0, args.interval - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        notes.append("bi dung som bang Ctrl+C")
    finally:
        if navigator:
            navigator[1]()
    if args.no_nav:
        notes.append("--no-nav: khong dieu huong")
    elif nav_done:
        notes.append(f"da dieu huong {nav_done}/{args.navigations} lan")
    write_summary(md_path, args, rows, notes, started)
    print(f"Xong. CSV: {csv_path}\nTom tat: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
