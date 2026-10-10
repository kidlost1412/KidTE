"""A/B: phát lại bình luận qua engine với cờ context_budget TẮT rồi BẬT, dùng responder THẬT.

    python backend/tools/context_ab.py --root <bản-sao-backend> [--limit 40] [--out ab_report]
                                       [--comments-file file.txt] [--mode ab|router]

--mode ab (mặc định): cờ context_budget TẮT vs BẬT (router tắt cả hai phía).
--mode router: cờ BẬT cả hai phía; "off" = router tắt, "on" = router bật. Token router báo riêng.

TỐN TIỀN: gọi DeepSeek thật (cần DEEPSEEK_API_KEY trong .env của bản sao). Công cụ này chỉ ĐỌC
customer_memory của bản sao để lấy ~40 câu được trả lời nhiều nhất; mỗi lượt dùng khách mới
(user id riêng) nên ký ức không lẫn giữa hai phía. Ghi <out>.json và <out>.md cạnh nhau.
Unit test dùng client giả qua hàm replay(); không bao giờ chạy với API thật trong test.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sim_common as sc  # noqa: E402

USAGE_KEYS = ("prompt_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "completion_tokens")
REGULAR_COMMENTS = ["hello", "ok", "có bán chịu không em", "toàn nói thật", "AI trả lời hả", "đắt quá bớt đi em"]
ROUTER_KEYS = ("router_prompt_tokens", "router_cache_hit_tokens", "router_completion_tokens")


def one_turn(engine, live_context, comment, tag, index, ident=None):
    started = time.time()
    ident = ident or {"user_id": f"ab-{tag}-{index:04d}", "unique_id": f"ab_{tag}_{index:04d}", "nickname": "Khách AB"}
    result = engine.handle({
        "comment": comment, "comment_id": f"ab-{tag}-{index}-{time.time_ns()}", **ident,
        "live_context": live_context, "received_at_ms": time.time() * 1000, "data_source": "ab"})
    metrics = result.get("metrics") or {}
    return {"reply": result.get("reply", "") if result.get("should_send") else "",
            "sent": bool(result.get("should_send")), "reason": result.get("reason", ""),
            "lane": metrics.get("context_lane", "full"),
            "usage": {key: int(metrics.get(key, 0) or 0) for key in USAGE_KEYS},
            "part_chars": metrics.get("context_part_chars") or {},
            "router": {key: int(metrics.get(key, 0) or 0) for key in ROUTER_KEYS},
            "router_ms": int(metrics.get("router_ms", 0) or 0),
            "router_used": bool(metrics.get("router_used")),
            "selection_source": metrics.get("selection_source", ""),
            "seconds": round(time.time() - started, 2)}


def replay(engine, live_context, comments, set_flags, regular=None):
    """set_flags(on: bool) chuyển cờ. Trả [{comment, off:{...}, on:{...}}].
    regular: (ident, [câu]) — phát thêm các câu dưới danh tính một khách quen THẬT của bản sao."""
    rows = []
    items = [(c, None) for c in comments] + [(c, regular[0]) for c in (regular[1] if regular else [])]
    for index, (comment, ident) in enumerate(items):
        row = {"comment": comment if ident is None else "[khách quen] " + comment}
        for tag, on in (("off", False), ("on", True)):
            set_flags(on)
            try:
                row[tag] = one_turn(engine, live_context, comment, tag, index, ident)
            except Exception as exc:  # noqa: BLE001 - một câu hỏng không làm hỏng cả báo cáo
                row[tag] = {"reply": "", "sent": False, "reason": f"error:{type(exc).__name__}", "lane": "",
                            "usage": {key: 0 for key in USAGE_KEYS}, "part_chars": {}, "seconds": 0,
                            "router": {key: 0 for key in ROUTER_KEYS}, "router_ms": 0, "router_used": False,
                            "selection_source": ""}
        rows.append(row)
    return rows


def summarize(rows):
    total = {"off": {key: 0 for key in USAGE_KEYS}, "on": {key: 0 for key in USAGE_KEYS}}
    for row in rows:
        for tag in total:
            for key in USAGE_KEYS:
                total[tag][key] += row[tag]["usage"].get(key, 0)
    # Token router tách riêng (không cộng vào prompt chính), mỗi phía.
    for tag in ("off", "on"):
        total[tag]["router"] = {key: sum(row[tag].get("router", {}).get(key, 0) for row in rows)
                                for key in ROUTER_KEYS}
        total[tag]["router_calls"] = sum(1 for row in rows if row[tag].get("router", {}).get("router_prompt_tokens"))
    return total


def to_markdown(rows, totals, mode="ab"):
    title = ("# A/B router: router TẮT vs BẬT (cờ ngữ cảnh bật cả hai)" if mode == "router"
             else "# A/B ngữ cảnh: cờ TẮT (cũ) vs BẬT (mới)")
    lines = [title, "",
             "| # | Bình luận | Làn | Prompt tắt/bật | Cache hit tắt/bật | Trả lời TẮT | Trả lời BẬT |",
             "|---|---|---|---|---|---|---|"]
    for i, row in enumerate(rows, 1):
        off, on = row["off"], row["on"]
        lines.append("| {} | {} | {} | {}/{} | {}/{} | {} | {} |".format(
            i, row["comment"].replace("|", "/"), on["lane"],
            off["usage"]["prompt_tokens"], on["usage"]["prompt_tokens"],
            off["usage"]["prompt_cache_hit_tokens"], on["usage"]["prompt_cache_hit_tokens"],
            (off["reply"] or f"[{off['reason']}]").replace("|", "/"),
            (on["reply"] or f"[{on['reason']}]").replace("|", "/")))
    lines += ["", "## Tổng", "", "| | prompt | cache hit | cache miss | completion |", "|---|---|---|---|---|"]
    for tag in ("off", "on"):
        t = totals[tag]
        lines.append(f"| {tag} | {t['prompt_tokens']} | {t['prompt_cache_hit_tokens']} | "
                     f"{t['prompt_cache_miss_tokens']} | {t['completion_tokens']} |")
    if mode == "router":
        r = totals["on"]["router"]
        lines += ["", "## Token router (báo riêng, phía BẬT)", "",
                  "| lần gọi | prompt | cache hit | completion |", "|---|---|---|---|",
                  f"| {totals['on']['router_calls']} | {r['router_prompt_tokens']} | "
                  f"{r['router_cache_hit_tokens']} | {r['router_completion_tokens']} |"]
        used = sum(1 for row in rows if row["on"].get("router_used"))
        lines += ["", f"Router đổi quyết định ở {used}/{len(rows)} câu."]
    return "\n".join(lines) + "\n"


def write_report(rows, out_base, mode="ab"):
    totals = summarize(rows)
    out_base = Path(out_base)
    out_base.with_suffix(".json").write_text(json.dumps({"rows": rows, "totals": totals},
                                                        ensure_ascii=False, indent=1), encoding="utf-8")
    out_base.with_suffix(".md").write_text(to_markdown(rows, totals, mode), encoding="utf-8")
    return totals


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, help="BẢN SAO thư mục backend (chứa data/ và .env)")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--comments-file", default="", help="mỗi dòng một bình luận (thay danh sách mặc định)")
    parser.add_argument("--out", default="context_ab_report")
    parser.add_argument("--mode", choices=("ab", "router"), default="ab")
    args = parser.parse_args(argv)
    root = sc.resolve_root(args.root)
    comments = ([ln.strip() for ln in Path(args.comments_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
                if args.comments_file else sc.frequent_comments(root, args.limit))
    if not comments:
        raise SystemExit("Không có bình luận nào để phát lại.")
    print(f"{len(comments)} bình luận × 2 lượt gọi DeepSeek THẬT (tốn tiền). Ctrl+C trong 5 giây để huỷ.", flush=True)
    time.sleep(5)
    flag_files = {True: sc.write_flags(True), False: sc.write_flags(False)}
    router_files = {True: sc.write_router_config(True), False: sc.write_router_config(False)}
    server, budget, engine = sc.boot_engine(root, real_key=True)
    import router as router_module

    def set_flags(on):
        if args.mode == "router":
            budget.FLAGS_PATH = flag_files[True]
            router_module.CONFIG_PATH = router_files[on]
        else:
            budget.FLAGS_PATH = flag_files[on]
            router_module.CONFIG_PATH = router_files[False]

    try:
        if getattr(engine.responder, "client", None) is None:
            raise SystemExit("Bản sao không có khóa DeepSeek (DEEPSEEK_API_KEY) — không gọi được responder thật.")
        rows = replay(engine, sc.live_context_from(root), comments, set_flags,
                     regular=(sc.top_customer(root), REGULAR_COMMENTS))
    finally:
        engine.close()
    totals = write_report(rows, args.out, args.mode)
    print(json.dumps(totals, ensure_ascii=False, indent=1))
    print(f"Đã ghi {args.out}.json và {args.out}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
