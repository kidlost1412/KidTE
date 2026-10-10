"""Nghe tieng video dang phat, suy ra dang o giay thu may, bao cho tien ich.

Cach dung:
  1) Liet ke thiet bi:   python backend/tools/list_audio_devices.py
  2) Do thu (khong gui): python plugins/dong-bo-video/video_sync.py --probe --device 23
  3) Chay that:          python plugins/dong-bo-video/video_sync.py --device 23

Chi gui ket qua khi do tin cay du cao, va yeu cau hai lan do lien tiep khop
nhau, nen mot doan nhac nen lap lai khong the lam tien ich nhay lung tung.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "src"))

from console_io import force_utf8_console  # noqa: E402

force_utf8_console()

import numpy as np  # noqa: E402

from audio_capture import RollingCapture, probe_all  # noqa: E402
from config import CHAT_HOST, CHAT_PORT, DATA_DIR  # noqa: E402
from video_fingerprint import (  # noqa: E402
    load_reference, locate, resample_mono, spectral_features,
)
from video_library import load_references  # noqa: E402

LEGACY_REFERENCE = DATA_DIR / "video_fingerprint.npz"
DEFAULT_STATUS_FILE = DATA_DIR.parent / "runtime" / "services" / "dong-bo-video.health.json"


def write_health(path, **changes):
    """Ghi snapshot health nguyên tử; lỗi health không làm chết nhận diện."""
    path = Path(path)
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(current, dict):
            current = {}
        current.update(changes)
        current["updated_at"] = datetime.now(timezone.utc).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
        return current
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def publish(position_s, confidence, margin, duration_s, video_id, host, port, quiet,
            min_margin=0.06):
    payload = {
        "video_id": video_id,
        "position_s": round(position_s, 2),
        "confidence": round(confidence, 4),
        "margin": round(margin, 4),
        # Gui kem nguong DANG dung, de backend mo ta muc chac chan bang loi
        # thay vi de giao dien tu quy margin thanh phan tram xac suat.
        "min_margin": round(float(min_margin), 4),
        "video_duration_s": round(duration_s, 3),
        "source": "audio_fingerprint",
    }
    request = urllib.request.Request(
        f"http://{host}:{port}/api/video/position",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Origin": f"http://{host}:{port}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            json.loads(response.read().decode("utf-8"))
        return True
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        if not quiet:
            print(f"  [khong gui duoc: {type(exc).__name__}] backend chua chay?")
        return False


def best_match(references, samples, rate):
    """So voi TAT CA van tay, tra ve video khop nhat.

    Nho vay khong can khai bao dang phat video nao: co bao nhieu video trong
    thu vien thi so ca bao nhieu, cai nao margin cao nhat thi lay.
    """
    features = spectral_features(samples, rate)
    best = None
    for reference in references:
        found = locate(reference["features"], features)
        if not found:
            continue
        start_s, score, margin = found
        if best is None or margin > best[3]:
            best = (reference, start_s, score, margin)
    return best


def autodetect(references, min_margin, seconds=6.0):
    """Tim thiet bi nao dang phat dung video, bang cach nghe thu tat ca.

    Ten thiet bi khac nhau tren tung may, va cach dinh tuyen am thanh cung co
    the doi bat cu luc nao, nen khong nen dong dinh mot cai ten nao ca.
    """
    print(f"Dang tu tim thiet bi ({seconds:g}s, nghe tat ca cung luc)...")
    best = None
    for item in probe_all(seconds=seconds):
        label = ("PHAT" if item["loopback"] else "THU ") + " " + item["name"][:44]
        if item["samples"] is None:
            print(f"  {label:52s} bo qua ({item['error']})")
            continue
        block = item["samples"]
        rms = float(np.sqrt(np.mean(block * block)) + 1e-12)
        db = 20 * np.log10(rms)
        if db < -70:
            print(f"  {label:52s} im lang ({db:.0f} dB)")
            continue
        samples, rate = resample_mono(block, 48000)
        found = best_match(references, samples, rate)
        if not found:
            print(f"  {label:52s} khong do duoc")
            continue
        reference, start_s, score, margin = found
        flag = "KHOP" if margin >= min_margin else "yeu "
        print(f"  {label:52s} {db:6.1f}dB margin={margin:.3f} {flag}"
              f" [{reference['video_id']}] (~{clock(start_s + seconds)})")
        if margin >= min_margin and (best is None or margin > best[1]):
            best = (item, margin)
    if not best:
        return None
    print(f"=> Chon: {best[0]['name']} (margin {best[1]:.3f})")
    return best[0]


def clock(seconds):
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"


def main_relock(args, references, last_device_id="", same_device_tries=0):
    """Chay lai khi mat dau, khong bat nguoi van hanh lam gi.

    Uu tien BAM LAI dung thiet bi vua chay duoc. Mat dau phan lon la vi chu
    phong noi de tieng video, hoac video tam dung — thiet bi khong he doi.
    Do lai ca dan thiet bi trong truong hop do vua vo ich vua danh thuc moi
    thiet bi USB tren may (sinh "USB device not recognized" giua live).

    Nhung neu bam lai dung thiet bi cu ma van mat dau, thi lan sau PHAI do
    lai that — khong duoc bam mai mot thiet bi da hong hoac da doi duong.
    """
    if last_device_id and same_device_tries < 1:
        print("Mat dau: bam lai dung thiet bi cu (khong do lai ca dan).")
        args.device = last_device_id
        return run(args, references, last_device_id=last_device_id,
                   same_device_tries=same_device_tries + 1)
    print("Bam lai thiet bi cu khong an, do lai toan bo duong phat...")
    args.device = "auto"
    return run(args, references)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default=(os.getenv("KID_TE_AUDIO_DEVICE", "") or os.getenv("LIVE_RE_AUDIO_DEVICE", "auto")),
                        help="auto (tu tim), hoac so thu tu, hoac mau ten vd VX2728")
    parser.add_argument("--reference", default="",
                        help="chi so voi mot van tay cu the (mac dinh: ca thu vien)")
    parser.add_argument("--window", type=float, default=8.0,
                        help="do dai cua so nghe, giay (benchmark dung 8s)")
    parser.add_argument("--interval", type=float, default=6.0)
    parser.add_argument("--min-margin", type=float, default=0.06,
                        help="duoi muc nay coi nhu khong chac, khong gui")
    parser.add_argument("--offset-ms", type=float, default=0.0,
                        help="bu tre duong truyen do duoc o buoc --probe")
    parser.add_argument("--probe", action="store_true", help="chi in, khong gui")
    parser.add_argument("--relock-after", type=int, default=5,
                        help="mat dau bao nhieu lan lien tiep thi tu tim lai")
    parser.add_argument("--rounds", type=int, default=0,
                        help="dung sau N lan do (0 = chay mai, dung cho live)")
    parser.add_argument("--host", default=CHAT_HOST)
    parser.add_argument("--port", type=int, default=CHAT_PORT)
    parser.add_argument("--status-file", default=str(DEFAULT_STATUS_FILE))
    args = parser.parse_args()

    references = []
    if args.reference:
        one = load_reference(args.reference)
        one["video_id"] = Path(args.reference).parent.name or "video"
        one["title"] = one["video_id"]
        references = [one]
    else:
        references = [item for item in load_references() if "error" not in item]
        for item in load_references():
            if "error" in item:
                print(f"  bo qua {item['video_id']}: {item['error']}")
        if not references and LEGACY_REFERENCE.exists():
            one = load_reference(LEGACY_REFERENCE)
            one["video_id"] = "video"
            one["title"] = "video"
            references = [one]
    if not references:
        write_health(args.status_file, state="error", reason="no_fingerprint")
        print("Chua co van tay nao. Tai video len trong Kid TE (Video & lich ghim) hoac: python backend/tools/add_video.py --help")
        return 2
    print("Thu vien van tay:")
    for item in references:
        print(f"  {item['video_id']:<14} {item['duration_s']:8.2f}s"
              f" · {item['features'].shape[0]} khung")
    return run(args, references)


def run(args, references, last_device_id="", same_device_tries=0):
    # .bat co the truyen vao chuoi rong hoac cap dau ngoac; dung de no roi
    # xuong nhanh tim theo ten roi bao loi kho hieu.
    selector = str(args.device or "").strip().strip('"').strip("'").strip()
    while selector.casefold() in ("auto", ""):
        chosen = autodetect(references, args.min_margin)
        if chosen:
            selector = chosen["id"]
            break
        write_health(args.status_file, state="waiting",
                     last_lost=datetime.now(timezone.utc).isoformat(),
                     reason="no_matching_audio_device")
        print("Chua co thiet bi nao dang phat video trong thu vien."
              " Van tiep tuc cho va tu quet lai...")
        # Do thu la lenh mot lan; service live thi phai song de nguoi van hanh
        # co the bat/doi video SAU khi worker da khoi dong.
        if args.probe or args.rounds > 0:
            return 2
        time.sleep(max(1.0, float(args.interval)))
    last_device_id = selector

    with RollingCapture(selector, args.window) as capture:
        write_health(args.status_file, state="running", device=capture.name,
                     device_id=selector, video_id="", margin=None,
                     last_seen="", last_lost="")
        print(f"Thiet bi: {capture.name}"
              f" · loopback={capture.loopback} · {capture.rate} Hz")
        print(f"Cua so {args.window:g}s, do moi {args.interval:g}s,"
              f" nguong margin {args.min_margin:g}"
              + (" · CHE DO DO THU" if args.probe else ""))
        previous = None
        sent = 0
        rounds = 0
        lost = 0
        while args.rounds <= 0 or rounds < args.rounds:
            rounds += 1
            time.sleep(args.interval)
            if capture.error:
                write_health(args.status_file, state="error", reason="audio_device_error",
                             last_lost=datetime.now(timezone.utc).isoformat())
                print(f"  LOI thiet bi: {capture.error}")
                break
            block = capture.latest()
            if block is None:
                print("  dang cho du du lieu am thanh...")
                continue
            rms = float(np.sqrt(np.mean(block * block)) + 1e-12)
            db = 20 * np.log10(rms)
            if db < -70:
                write_health(args.status_file, state="waiting", reason="audio_silent",
                             last_lost=datetime.now(timezone.utc).isoformat())
                print(f"  {db:6.1f} dBFS - thiet bi nay dang IM LANG")
                lost += 1
                if args.relock_after and lost >= args.relock_after:
                    print("Im lang qua lau, tu tim lai thiet bi...")
                    return main_relock(args, references, last_device_id,
                                       same_device_tries)
                continue
            samples, rate = resample_mono(block, capture.rate)
            found = best_match(references, samples, rate)
            if not found:
                write_health(args.status_file, state="waiting", reason="no_fingerprint_match",
                             last_lost=datetime.now(timezone.utc).isoformat())
                print("  khong tinh duoc dac trung")
                continue
            reference, start_s, score, margin = found
            position = start_s + args.window + args.offset_ms / 1000.0
            position %= max(1.0, reference["duration_s"])
            ok = margin >= args.min_margin
            agree = (previous is not None
                     and abs((position - previous[0])
                             - (time.monotonic() - previous[1])) < 2.5)
            status = "OK " if ok else "YEU"
            stamp = datetime.now(timezone.utc).isoformat()
            write_health(
                args.status_file, state="running" if ok else "waiting",
                video_id=reference["video_id"], margin=round(margin, 4),
                device=capture.name,
                **({"last_seen": stamp, "reason": ""} if ok
                   else {"last_lost": stamp, "reason": "low_margin"}),
            )
            print(f"  [{reference['video_id']}] {clock(position)}"
                  f"  score={score:.3f} margin={margin:.3f}"
                  f" muc={db:6.1f}dB  {status}"
                  f"{' · khop lan truoc' if agree else ''}")
            if ok and not args.probe and (agree or previous is None):
                if publish(position, score, margin, reference["duration_s"],
                           reference["video_id"],
                           args.host, args.port, quiet=False,
                           min_margin=args.min_margin):
                    sent += 1
                    print(f"    da bao cho tien ich (lan thu {sent})")
            if ok:
                previous = (position, time.monotonic())
                lost = 0
                # Bam duoc roi thi lan mat dau sau lai duoc quyen thu cach re
                # (bam lai thiet bi cu) truoc khi do lai ca dan.
                same_device_tries = 0
            else:
                lost += 1
                if args.relock_after and lost >= args.relock_after:
                    print("Mat dau qua lau, tu tim lai thiet bi...")
                    return main_relock(args, references, last_device_id,
                                       same_device_tries)
        if args.rounds > 0:
            print(f"Da do {rounds} lan roi dung (--rounds {args.rounds}).")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDa dung.")
