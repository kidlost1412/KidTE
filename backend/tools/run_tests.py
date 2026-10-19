"""Điểm chạy test duy nhất; Node chạy test giao diện và hai tiện ích Chrome."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
EXTENSION = ROOT / "extension"
SHOPEE_EXTENSION = ROOT / "extension-shopee"

# Khoá theo TÊN chứ không theo số lượng: một suite bị xoá và một suite khác
# được thêm vào sẽ giữ nguyên tổng số, nên đếm không phát hiện ra.
EXPECTED_EXTENSION_TESTS = {
    "test-api-guard.cjs",
    "test-auto-start.cjs",
    "test-background-api.cjs",
    "test-browser-lease.cjs",
    "test-collector.cjs",
    "test-comments.cjs",
    "test-message-delivery.cjs",
    "test-content.cjs",
    "test-popup.cjs",
    "test-schedule.cjs",
    "test-timeline.cjs",
    "test-video2-timeline.cjs",
}
EXPECTED_SHOPEE_TESTS = {"test-ai-controller.cjs", "test-background.cjs", "test-chat.cjs",
                         "test-core.cjs", "test-shopee.cjs", "test-popup.cjs", "test-controller.cjs", "test-context-lifecycle.cjs"}


def run(command, cwd):
    print("TEST:", " ".join(str(item) for item in command), flush=True)
    return subprocess.run(command, cwd=cwd, check=False).returncode


def main():
    parser = argparse.ArgumentParser(description="Chạy test Kid TE")
    parser.add_argument("--suite", choices=("all", "python", "web", "extension"), default="all",
                        help="python: backend · web: mô hình giao diện (node --test) · extension: tiện ích Chrome")
    parser.add_argument("--node", default="", help="đường dẫn Node cho test giao diện và tiện ích")
    args = parser.parse_args()
    if args.suite in {"all", "python"}:
        code = run([sys.executable, "-m", "pytest", "tests", "-q"], BACKEND)
        if code:
            return code
    if args.suite in {"all", "web"}:
        node = args.node or shutil.which("node")
        if not node:
            print("THIẾU NODE CHO TEST GIAO DIỆN. Dùng --node <đường-dẫn-node>.")
            return 2
        tests = sorted(str(p) for p in (BACKEND / "tests" / "js").glob("*.test.mjs"))
        code = run([node, "--test", *tests], ROOT)
        if code:
            return code
    if args.suite in {"all", "extension"}:
        node = args.node or shutil.which("node")
        if not node:
            print("THIẾU NODE CHO TEST EXTENSION. Dùng --node <đường-dẫn-node>.")
            return 2
        tests = sorted(EXTENSION.glob("test-*.cjs"))
        thieu = sorted(EXPECTED_EXTENSION_TESTS - {item.name for item in tests})
        if thieu:
            print("THIẾU SUITE TEST EXTENSION: " + ", ".join(thieu))
            return 2
        for test in tests:
            code = run([node, str(test)], EXTENSION)
            if code:
                return code
        shopee_tests = sorted(SHOPEE_EXTENSION.glob("test-*.cjs"))
        thieu = sorted(EXPECTED_SHOPEE_TESTS - {item.name for item in shopee_tests})
        if thieu:
            print("THIẾU SUITE TEST SHOPEE: " + ", ".join(thieu))
            return 2
        for test in shopee_tests:
            code = run([node, str(test)], SHOPEE_EXTENSION)
            if code:
                return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
