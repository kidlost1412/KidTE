"""Liet ke thiet bi am thanh + do muc tin hieu, de chon dung nguon bat tieng video."""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from console_io import force_utf8_console  # noqa: E402

force_utf8_console()

import numpy as np  # noqa: E402

from audio_capture import RollingCapture, list_devices  # noqa: E402


def show():
    print("Thiet bi co the bat tieng video (dung so ben trai lam --device):\n")
    for index, item in enumerate(list_devices()):
        kind = "PHAT (loopback)" if item["loopback"] else "THU (mic/line)"
        print(f"  [{index}] {kind:16s} {item['name']}")
    print("\nTieng video phat ra man hinh/loa thi chon dong PHAT (loopback).")


def level(selector, seconds):
    with RollingCapture(selector, window_s=0.5) as capture:
        print(f"Dang do: {capture.name} (loopback={capture.loopback})")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if capture.error:
                print("LOI:", capture.error)
                return
            block = capture.latest()
            if block is None:
                print("  dang cho du du lieu...")
                continue
            rms = float(np.sqrt(np.mean(block * block)) + 1e-12)
            db = 20 * np.log10(rms)
            bar = "#" * max(0, min(40, int((db + 60) * 40 / 60)))
            print(f"  {db:7.1f} dBFS |{bar:<40}|", flush=True)
    print("Neu tat ca khoang -120 dBFS thi thiet bi nay KHONG co tieng video.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--level", metavar="DEVICE")
    parser.add_argument("--seconds", type=float, default=5.0)
    args = parser.parse_args()
    if args.level is None:
        show()
    else:
        level(args.level, args.seconds)
