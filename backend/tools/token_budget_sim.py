"""Đo token từng phần prompt trên BẢN SAO dữ liệu; chặn gọi DeepSeek (client giả).

    python backend/tools/token_budget_sim.py --root <bản-sao-backend> [--platform tiktok|shopee]
                                             [--flags on|off|both]

Từ chối chạy trực tiếp trên D:\\KidTE\\DuLieu (cần một bản sao). Ước lượng token = ký tự / 2,6.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim_common as sc  # noqa: E402

QUESTIONS = ["hello", "ok", "giá bao nhiêu ạ?", "kích rể xịt lên lá đc ko e",
             "5 lít pha tưới bao nhiêu khối nước em", "có bán chịu không em",
             "sao cà nhà mình gần 3th lại bị xoắn lá nhỉ", "1 chai kich rễ pha như nào",
             "ok lấy 2 chai", "[laugh]"]


def router_row(router_sink, before):
    """Cỡ lời gọi router của lượt vừa chạy (ký tự): cố định (hệ thống+danh mục) và riêng lượt."""
    calls = router_sink[before:]
    if not calls:
        return {"router_chars": 0, "router_stable_chars": 0, "router_turn_chars": 0}
    msgs = calls[0]["messages"]
    stable = sum(len(m["content"]) for m in msgs[:-1])
    turn = len(msgs[-1]["content"])
    return {"router_chars": stable + turn, "router_stable_chars": stable, "router_turn_chars": turn}


def run_tiktok(engine, live_context, customers, captured, questions, router_sink=None):
    rows, previous = [], None
    n = 0
    for who, ident in customers.items():
        for q in questions:
            n += 1
            before = len(captured)
            rbefore = len(router_sink) if router_sink is not None else 0
            result = engine.handle({"comment": q, "comment_id": f"sim-{who}-{n}-{time.time()}", **ident,
                                    "live_context": live_context, "received_at_ms": time.time() * 1000,
                                    "data_source": "sim"})
            calls = captured[before:]
            row = {"who": who, "q": q, "reason": result.get("reason", ""), "calls": len(calls), "parts": {},
                   "chars": 0, "shared_chars": 0}
            if router_sink is not None:
                row.update(router_row(router_sink, rbefore))
            if calls:
                msgs = calls[0]["messages"]
                row["parts"] = sc.message_parts(msgs)
                row["chars"] = sum(len(m["content"]) for m in msgs)
                if previous is not None:
                    row["shared_chars"], _ = sc.common_prefix_chars(previous, msgs)
                previous = msgs
            rows.append(row)
    return rows


def run_shopee(engine, captured, questions, router_sink=None):
    state = engine.shopee_mapping_state()
    videos = state.get("videos", {})
    products = state.get("catalog", {}).get("products", [])
    chosen = None
    for video_id, video in videos.items():
        allowed = {f"{r['shop_id']}:{r['product_id']}" for src in (video.get("mappings", {}), video.get("alternates", {}))
                   for r in src.values() if isinstance(r, dict) and r.get("shop_id") and r.get("product_id")}
        for item in products:
            if f"{item.get('shop_id')}:{item.get('product_id')}" in allowed:
                chosen = (video_id, {"shop_id": item["shop_id"], "product_id": item["product_id"]}, item.get("title", ""))
                break
        if chosen:
            break
    if not chosen:
        raise SystemExit("Bản sao không có ánh xạ Shopee khớp danh mục (shopee_live_mappings.json) — không đo được Shopee.")
    video_id, mapped, title = chosen
    ai = engine.shopee_ai()
    rows, previous = [], None
    for who, user in (("new", "9" + "0" * 9 + "1"), ("regular", "9" + "0" * 9 + "2")):
        for i, q in enumerate(questions):
            before = len(captured)
            rbefore = len(router_sink) if router_sink is not None else 0
            result = ai.reply({"session_id": "1234567", "video_id": video_id,
                               "comment_id": f"sim-{who}-{i}-{time.time_ns()}", "user_id": user,
                               "nickname": "Khách Sim " + who, "comment": q,
                               "shop_id": str(mapped["shop_id"]), "product_id": str(mapped["product_id"]),
                               "title": title, "received_at_ms": time.time() * 1000})
            calls = captured[before:]
            row = {"who": who, "q": q, "reason": result.get("reason", ""), "calls": len(calls), "parts": {},
                   "chars": 0, "shared_chars": 0}
            if router_sink is not None:
                row.update(router_row(router_sink, rbefore))
            if calls:
                msgs = calls[0]["messages"]
                row["parts"] = sc.message_parts(msgs)
                row["chars"] = sum(len(m["content"]) for m in msgs)
                if previous is not None:
                    row["shared_chars"], _ = sc.common_prefix_chars(previous, msgs)
                previous = msgs
            rows.append(row)
    return rows


def run_mode(root, platform, on, questions, with_router=False, c_on=None):
    flags = sc.write_flags(on, c_on=c_on)
    server, _budget, engine = sc.boot_engine(root, flags)
    captured, router_sink = [], ([] if with_router else None)
    responder = engine.responder
    responder.client = sc.fake_client(captured, router_sink=router_sink)
    responder.check_balance = lambda *a, **k: {"ok": True, "deepseek_available": True}
    if with_router:
        import router
        router.CONFIG_PATH = sc.write_router_config(True)
    try:
        if platform == "shopee":
            return run_shopee(engine, captured, questions, router_sink)
        live_context = sc.live_context_from(root)
        customers = {"new": {"user_id": "sim-new-0001", "unique_id": "khach_moi_sim", "nickname": "Khách Mới"},
                     "regular": sc.top_customer(root)}
        return run_tiktok(engine, live_context, customers, captured, questions, router_sink)
    finally:
        engine.close()


def print_table(title, rows, detail_min=120):
    print(f"\n=== {title} ===")
    has_router = any("router_chars" in row for row in rows)
    print(f"{'khách':8} {'câu hỏi':44} {'gọi':>3} {'sys':>6} {'stable':>7} {'turn':>6} {'TỔNG':>6} {'dùng chung tiền tố':>20}"
          + (f" {'router(cố định+lượt)':>22}" if has_router else ""))
    for row in rows:
        parts = row["parts"]
        system = parts.get("system_prompt", 0)
        stable = sum(v for k, v in parts.items() if k.startswith("stable."))
        turn = sum(v for k, v in parts.items() if k.startswith("turn."))
        share = f"{sc.tok(row['shared_chars'])} ({row['shared_chars'] * 100 // max(1, row['chars'])}%)" if row["chars"] else "-"
        print(f"{row['who']:8} {row['q'][:44]:44} {row['calls']:>3} {sc.tok(system):>6} {sc.tok(stable):>7} "
              f"{sc.tok(turn):>6} {sc.tok(row['chars']):>6} {share:>20}"
              + (f" {sc.tok(row['router_chars']):>8} ({sc.tok(row['router_stable_chars'])}+{sc.tok(row['router_turn_chars'])})"
                 if has_router else "")
              + ("" if row["calls"] else f"  [{row['reason']}]"))
        big = sorted(((k, v) for k, v in parts.items() if k != "system_prompt" and v >= detail_min),
                     key=lambda kv: -kv[1])
        if big:
            print("         " + ", ".join(f"{k}:{sc.tok(v)}" for k, v in big))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, help="BẢN SAO thư mục backend (chứa data/)")
    parser.add_argument("--platform", choices=("tiktok", "shopee"), default="tiktok")
    parser.add_argument("--flags", choices=("on", "off", "both"), default="both")
    parser.add_argument("--router", action="store_true",
                        help="bật router (client giả trả phán quyết trung tính) và in cỡ lời gọi router")
    parser.add_argument("--c-flags", choices=("on", "off"), default="",
                        help="cờ Gói C (làn giữ tính cách, thẻ khách, sổ tay); mặc định theo --flags")
    parser.add_argument("--compare", choices=("a", "c"), default="a",
                        help="a: Gói A tắt→bật (mặc định); c: Gói A bật, Gói C tắt→bật (so sánh riêng Gói C)")
    parser.add_argument("--json", default="", help="ghi kết quả thô ra file JSON")
    parser.add_argument("--question", action="append", help="thay danh sách câu hỏi mặc định (lặp được)")
    args = parser.parse_args(argv)
    root = sc.resolve_root(args.root)
    questions = args.question or QUESTIONS
    modes = {"on": [True], "off": [False], "both": [False, True]}[args.flags]
    if args.compare == "c":
        modes = [False, True]       # ở chế độ này False/True = Gói C tắt/bật; Gói A luôn bật
    if len(modes) > 1:
        # Mỗi chế độ cần tiến trình sạch (config nạp một lần); chạy lần lượt trong tiến trình con.
        import subprocess
        results = {}
        for on in modes:
            out = Path(sc.write_flags(on).parent) / "rows.json"
            # Bản sao riêng cho mỗi chế độ: bộ nhớ khách bị ghi thêm khi chạy, hai lần chạy phải cùng đầu vào.
            work = sc.scratch_dir("tokensim-") / "root"
            shutil.copytree(root, work)
            cmd = [sys.executable, __file__, "--root", str(work), "--platform", args.platform,
                   "--flags", "on" if (on or args.compare == "c") else "off", "--json", str(out)]
            if args.compare == "c":
                cmd += ["--c-flags", "on" if on else "off"]
            if args.router:
                cmd.append("--router")
            for q in args.question or []:
                cmd += ["--question", q]
            code = subprocess.run(cmd, check=False).returncode
            shutil.rmtree(work.parent, ignore_errors=True)
            if code:
                return code
            results[on] = json.loads(out.read_text(encoding="utf-8"))
        print("\n=== TRƯỚC (Gói C tắt) → SAU (Gói C bật), ước lượng token ===" if args.compare == "c"
              else "\n=== TRƯỚC (cờ tắt) → SAU (cờ bật), ước lượng token ===")
        print(f"{'khách':8} {'câu hỏi':44} {'trước':>7} {'sau':>7} {'giảm':>6}")
        total_before = total_after = 0
        for old, new in zip(results[False], results[True]):
            before, after = sc.tok(old["chars"]), sc.tok(new["chars"])
            total_before += before
            total_after += after
            print(f"{old['who']:8} {old['q'][:44]:44} {before:>7} {after:>7} {(before - after) * 100 // max(1, before):>5}%")
        print(f"{'TỔNG':53} {total_before:>7} {total_after:>7} {(total_before - total_after) * 100 // max(1, total_before):>5}%")
        if args.json:
            Path(args.json).write_text(json.dumps({"off": results[False], "on": results[True]},
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    c_on = None if not args.c_flags else args.c_flags == "on"
    rows = run_mode(root, args.platform, modes[0], questions, with_router=args.router, c_on=c_on)
    print_table(f"{args.platform} — cờ {'BẬT' if modes[0] else 'TẮT'}"
                + ("" if c_on is None else f" · Gói C {'BẬT' if c_on else 'TẮT'}"), rows)
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
