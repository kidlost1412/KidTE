"""Checklist tuong tac cho nguoi van hanh chay phien live that (T05).

Chi doc: GET http://127.0.0.1:8793 va doc file log. KHONG POST, KHONG gui chat.
  python tools/live_checklist.py --list
  python tools/live_checklist.py [--root DIR] [--out DIR]
  python tools/live_checklist.py --resume test-artifacts/live/<ts>.json
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_URL = "http://127.0.0.1:8793"
RESULTS = ("pass", "fail", "skip")
_ALIASES = {"p": "pass", "pass": "pass", "d": "pass", "f": "fail", "fail": "fail",
            "s": "skip", "skip": "skip", "b": "skip"}


def _s(id, title, do, observe, api=(), files=()):
    return {"id": id, "title": title, "instructions": do, "observe": observe,
            "api": list(api), "files": list(files)}


STEPS = [
    _s("send_real", "Gửi thật TikTok/Shopee (1-3 tin)",
       "Trên TikTok và Shopee, bình luận thật để bot trả lời 1-3 tin. Mỗi tin <= 150 ký tự UTF-16 và có tag người hỏi.",
       "Tin xuất hiện đúng 1 lần trên sàn, đúng độ dài, có tag; dashboard ghi đã gửi.",
       ["/api/dashboard", "/api/shopee/ai/status"], ["logs/*.log"]),
    _s("receipt_unknown", "Biên nhận / unknown",
       "Quan sát biên nhận sau khi gửi; mô phỏng mất biên nhận (đóng tab trước khi extension báo).",
       "Tin có biên nhận = đã gửi; không có = 'unknown' (không tự gửi lại, chờ xác minh).",
       ["/api/dashboard"], ["logs/*.log"]),
    _s("no_dup_tab_reload", "Không gửi trùng khi tải lại tab",
       "Đang có tin chờ gửi, tải lại (F5) tab live TikTok/Shopee.",
       "Không có tin trùng trên sàn sau khi tải lại.", ["/api/dashboard"]),
    _s("no_dup_ext_reload", "Không gửi trùng khi tải lại extension",
       "Vào chrome://extensions bấm tải lại extension giữa lúc có tin chờ gửi.",
       "Không có tin trùng.", ["/api/dashboard"]),
    _s("no_dup_restart", "Không gửi trùng khi khởi động lại KidTE",
       "Tắt và mở lại KidTE khi còn tin chưa xác nhận.",
       "Tin chưa rõ ở trạng thái unknown, không gửi lại; không trùng trên sàn.",
       ["/api/dashboard"], ["logs/*.log"]),
    _s("queue_mode", "Công tắc queue mode bật/tắt",
       "Bật rồi tắt queue mode trên giao diện; kiểm tra file data/machine/queue_mode.json.",
       "File đổi theo công tắc; bật: bình luận xếp hàng đợi; tắt: xử lý trực tiếp.",
       ["/api/settings", "/api/dashboard"], ["data/machine/queue_mode.json"]),
    _s("video_lock_switch", "Khóa video, đổi video giữa live, khởi động lại",
       "Khóa video đang bán; đổi sang video khác giữa live; khởi động lại KidTE.",
       "Sau restart khôi phục video khóa ở trạng thái 'uncertain' rồi xác minh lại (re-verify).",
       ["/api/video/analysis/status", "/api/dashboard"]),
    _s("obs_shopee_file", "OBS thay file Shopee cùng tên",
       "Thay file ảnh/video nguồn OBS của Shopee bằng file khác cùng tên.",
       "Hệ thống nhận ra nội dung đổi (không dựa vào tên file).", ["/api/live/sources"]),
    _s("captcha_7", "CAPTCHA tối đa 7 lần rồi báo Telegram",
       "Gặp CAPTCHA thật; để solver thử. Nếu thất bại liên tiếp.",
       "Tối đa 7 lần thử; sau đó Telegram báo thủ công, dừng tự giải.",
       ["/api/captcha"], ["captcha/*.log", "captcha/tiktok-captcha-solver-logs/*.log"]),
    _s("sadcaptcha_401", "SadCaptcha 401 gửi cảnh báo",
       "Dùng tạm khóa SadCaptcha sai (bản sao cấu hình) hoặc để hết hạn.",
       "Cảnh báo 401 gửi Telegram, không thử vô hạn.", ["/api/captcha"], ["captcha/*.log", "captcha/tiktok-captcha-solver-logs/*.log"]),
    _s("recording_cleanup", "Dọn bản ghi chưa gửi sau 30 phút",
       "Để bản ghi chưa gửi quá 30 phút.", "Bản ghi bị dọn; ghi log dọn dẹp.",
       ["/api/dashboard"], ["logs/*.log"]),
    _s("privacy_forget", "Quên tôi đi chặn AI",
       "Khách bình luận 'quên tôi đi', sau đó bình luận tiếp.",
       "Yêu cầu quên được ghi nhận; AI không trả lời/không lưu khách đó.",
       ["/api/dashboard"], ["logs/*.log"]),
    _s("backup_v2_startup", "Sao lưu v2 tự động khi khởi động",
       "Khởi động KidTE và chờ vài phút.", "Có bản sao lưu v2 mới khi khởi động.",
       ["/api/backup"], ["logs/*.log"]),
    _s("dashboard_ai_queue", "Bảng hàng đợi AI trên dashboard",
       "Mở dashboard khi có bình luận đang xử lý.",
       "Bảng hàng đợi AI hiện đúng số lượng, cập nhật theo thời gian.", ["/api/dashboard"]),
]


def list_steps() -> str:
    return "\n".join(f"{i:2d}. [{st['id']}] {st['title']}" for i, st in enumerate(STEPS, 1))


def http_get(path: str, timeout: float = 5.0) -> dict:
    """Chi GET toi 127.0.0.1:8793/api/..."""
    if not path.startswith("/api/"):
        raise ValueError("chi cho phep duong dan /api/")
    req = urllib.request.Request(BASE_URL + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            status = getattr(r, "status", 200)
        try:
            body = json.loads(body)
        except ValueError:
            pass
        return {"path": path, "status": status, "body": body}
    except Exception as e:  # noqa: BLE001
        return {"path": path, "error": f"{type(e).__name__}: {e}"}


def tail_files(root: Path, patterns, lines: int = 8) -> dict:
    out = {}
    for pat in patterns:
        for f in sorted(Path(root).glob(pat))[-2:]:
            if f.is_file():
                try:
                    txt = f.read_text("utf-8", "replace").splitlines()[-lines:]
                except OSError as e:
                    txt = [f"loi doc: {e}"]
                out[str(f)] = txt
    return out


def new_state(started: str | None = None) -> dict:
    return {"started": started or datetime.now().isoformat(timespec="seconds"),
            "results": {}}


def record(state: dict, step_id: str, result: str, note: str = "") -> None:
    if step_id not in {s["id"] for s in STEPS}:
        raise KeyError(step_id)
    if result not in RESULTS:
        raise ValueError(result)
    state["results"][step_id] = {"result": result, "note": note,
                                 "at": datetime.now().isoformat(timespec="seconds")}


def render_md(state: dict) -> str:
    lines = [f"# Kết quả live checklist ({state['started']})", "",
             "| # | Bước | Kết quả | Ghi chú |", "|---|---|---|---|"]
    for i, st in enumerate(STEPS, 1):
        r = state["results"].get(st["id"])
        res = r["result"] if r else "chua lam"
        note = (r["note"] if r else "").replace("|", "/").replace("\n", " ")
        lines.append(f"| {i} | {st['title']} | {res} | {note} |")
    return "\n".join(lines) + "\n"


def save(state: dict, path: Path) -> tuple[Path, Path]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    md = path.with_suffix(".md")
    md.write_text(render_md(state), "utf-8")
    return path, md


def run(state: dict, out_json: Path, root: Path, ask=input, say=print) -> dict:
    for i, st in enumerate(STEPS, 1):
        if st["id"] in state["results"]:
            continue
        say(f"\n=== Bước {i}/{len(STEPS)}: {st['title']} ===")
        say("Làm: " + st["instructions"])
        say("Quan sát: " + st["observe"])
        for p in st["api"]:
            r = http_get(p)
            say(f"[GET {p}] " + json.dumps(r, ensure_ascii=False)[:600])
        for f, ls in tail_files(root, st["files"]).items():
            say(f"[log {f}]\n  " + "\n  ".join(ls))
        while True:
            a = ask("Kết quả (p=đạt / f=hỏng / s=bỏ qua / q=thoát và lưu): ").strip().lower()
            if a == "q":
                save(state, out_json)
                return state
            if a in _ALIASES:
                break
        note = ask("Ghi chú: ").strip()
        record(state, st["id"], _ALIASES[a], note)
        save(state, out_json)
    save(state, out_json)
    return state


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--resume")
    ap.add_argument("--root", default=".", help="thu muc chua data/ (de doc log)")
    ap.add_argument("--out", default="test-artifacts/live")
    a = ap.parse_args(argv)
    if a.list:
        print(list_steps())
        return 0
    if a.resume:
        p = Path(a.resume)
        state = json.loads(p.read_text("utf-8"))
    else:
        state = new_state()
        p = Path(a.out) / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    run(state, p, Path(a.root))
    print(f"Đã lưu: {p} và {p.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
