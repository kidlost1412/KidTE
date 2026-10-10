"""T02: replay the A03 conversation fixtures through the REAL reply engines and score them.

Default (and what the tests use): a deterministic FakeModel, no network.
Operator run against the configured model:

    ..\\.venv\\Scripts\\python.exe tools\\eval_conversations.py --real            (prints the estimate, stops)
    ..\\.venv\\Scripts\\python.exe tools\\eval_conversations.py --real --yes       (really calls the model)

All engine state lives in a temp root; the operator's data is never opened.
The model key is only READ from the operator's .env (the DEEPSEEK_* values
the existing model-deepseek plugin reads) and is never written or printed.

Scoring per turn: expected facts present (expect.facts + facts implied by the
verified context: quotable price, verified dose numbers), no severe error
(price not matching the SKU's quotable price, dose number absent from
verified_dose_calculations, product not in catalog), length <= 150 UTF-16
including the @tag. Pass = >= 90% of turns and 0 severe errors.
"""
from __future__ import annotations

import argparse
import atexit
import shutil
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
FIXTURES = BACKEND / "tests" / "fixtures" / "conversations"
CATALOG = BACKEND / "tests" / "fixtures" / "eval_catalog.json"
PASS_RATE = 0.90
if str(BACKEND / "src") not in sys.path:
    sys.path.insert(0, str(BACKEND / "src"))
MONEY = re.compile(r"(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?\s*k\b|\d{4,})\s*(?:₫|đ|vnd|vnđ|đồng)?", re.I)
DOSE = re.compile(r"(\d+(?:[.,]\d+)?)(?:\s*[-–]\s*(\d+(?:[.,]\d+)?))?\s*(?:ml|kg|gam|gram|g)\b", re.I)
CODE = re.compile(r"\b(?:[A-Z]{2,4}-?\d{2,3})\b")


def utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def money_values(text):
    out = []
    for m in MONEY.finditer(text or ""):
        raw = m.group(1).lower().replace(" ", "")
        try:
            if raw.endswith("k"):
                out.append(int(float(raw[:-1].replace(",", ".")) * 1000))
            elif re.search(r"[.,]\d{3}\b", raw):
                out.append(int(re.sub(r"[.,]", "", raw)))
            else:
                out.append(int(float(raw.replace(",", "."))))
        except ValueError:
            pass
    return out


def load_catalog(path=CATALOG):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def vnd(n):
    return f"{int(n):,}".replace(",", ".") + "₫"


# ----------------------------------------------------------------- scoring
def score_turn(turn, observed, catalog):
    """observed: {sent, bodies, tag, allowed_prices, dose_rows, price_asked, dose_asked, reason}."""
    expect = turn.get("expect", {})
    intent = expect.get("intent", "")
    bodies = observed.get("bodies") or []
    text = " ".join(bodies)
    severe, problems = [], []
    tag = observed.get("tag", "")
    for body in bodies:
        if utf16_len(f"{tag} {body}".strip()) > 150:
            problems.append("length>150")
    if bodies:
        allowed = set(observed.get("allowed_prices") or ())
        for value in money_values(text):
            if value not in allowed:
                severe.append(f"wrong_price:{value}")
        allowed_dose = set()
        for row in observed.get("dose_rows") or []:
            for key in ("calculated_ml_min", "calculated_ml_max"):
                if key in row:
                    allowed_dose.add(round(float(row[key]), 2))
        for m in DOSE.finditer(text):
            for part in m.groups():
                if part is not None and round(float(part.replace(",", ".")), 2) not in allowed_dose:
                    severe.append(f"dose_not_verified:{part}")
        known = {str(p.get("id", "")).upper() for p in catalog["products"]}
        for p in catalog["products"]:
            known |= {w.upper() for w in re.findall(r"[A-Za-z]{2,4}-?\d{2,3}", p["title"] + " " + p.get("aliases", ""))}
        for code in CODE.findall(text):
            if code.upper() not in known:
                severe.append(f"invented_product:{code}")
        titles = " ".join(p["title"].casefold() for p in catalog["products"])
        for phrase in re.findall(r"nano tonic|siêu kích|super[ -]?grow", text.casefold()):
            if phrase not in titles:
                severe.append(f"invented_product:{phrase}")
    facts = list(expect.get("facts") or [])
    if not facts:
        asks = re.search(r"giá|gia|bao nhiêu tiền|bao nhieu tien", turn.get("comment", "").casefold())
        if intent == "price" and asks and observed.get("price_asked") and observed.get("allowed_prices"):
            facts.append(vnd(sorted(observed["allowed_prices"])[0]))
        if intent == "dose" and observed.get("dose_rows"):
            facts.append(f"{float(observed['dose_rows'][0]['calculated_ml_min']):g}")
    if intent == "forget-me" and not bodies and observed.get("reason") != "memory_deleted":
        problems.append("forget_not_acknowledged")
    squash = lambda s: re.sub(r"[.,\s₫đ]|vnd", "", s.casefold())  # noqa: E731
    for needle in facts:
        if bodies:
            if needle.casefold() not in text.casefold() and squash(needle) not in squash(text):
                problems.append(f"missing_fact:{needle}")
        elif intent in ("price", "dose"):
            problems.append(f"no_reply_missing_fact:{needle}")
    return {"passed": not severe and not problems, "severe": severe, "problems": problems}


# ------------------------------------------------------------------ models
class CallBudget(Exception):
    pass


class RecordingModel:
    """Wraps any responder; records contexts/replies per turn and enforces --max-calls."""

    def __init__(self, inner, max_calls=None):
        self.inner = inner
        self.max_calls = max_calls
        self.calls = 0
        self.turn_log = []

    def start_turn(self):
        self.turn_log = []

    def reply(self, nickname, comment, context, max_chars, **kwargs):
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise CallBudget(f"max_calls {self.max_calls} reached")
        self.calls += 1
        result = self.inner.reply(nickname, comment, context, max_chars, **kwargs)
        self.turn_log.append({"context": context, "result": result, "comment": comment})
        return result

    def __getattr__(self, name):
        return getattr(self.inner, name)


class FakeModel:
    """Deterministic, context-driven. `fault` injects a severe error for harness tests:
    wrong_price | bad_dose | invent_product."""

    def __init__(self, fault=None):
        self.fault = fault

    def check_balance(self):
        return {"ok": True, "service": "fake"}

    def reply(self, nickname, comment, context, max_chars, **kwargs):
        low = comment.casefold()
        pc = context.get("price")
        price = pc.get("quotable_price") if isinstance(pc, dict) else ""
        rows = context.get("verified_dose_calculations") or []
        if self.fault == "invent_product":
            return {"action": "reply", "reply": "Dạ bên em có Nano Tonic NT99 rất tốt nha", "metrics": {}}
        if self.fault == "wrong_price" and price:
            return {"action": "reply", "reply": "Dạ giá 999.000₫ nha", "metrics": {}}
        if self.fault == "bad_dose" and rows:
            return {"action": "reply", "reply": "Dạ pha 77ml cho bình nha", "metrics": {}}
        if rows:
            row = rows[0]
            return {"action": "reply", "reply": f"Dạ pha {float(row['calculated_ml_min']):g}ml cho bình {float(row['target_tank_liters']):g} lít nha", "metrics": {}}
        if price and re.search(r"giá|bao nhiêu|bao nhieu|gia ", low):
            return {"action": "reply", "reply": f"Dạ giá {price} nha", "metrics": {}}
        return {"action": "reply", "reply": "Dạ em tư vấn theo hướng dẫn trên nhãn nha", "metrics": {}}


# ------------------------------------------------------------------ harness
def _seed_root_env(real):
    """Run before importing server/config. Real mode copies only the DEEPSEEK_* env values."""
    if real:
        try:
            code = "import sys;sys.path.insert(0,sys.argv[1]);import config;print(config.ROOT)"
            out = subprocess.run([sys.executable, "-c", code, str(BACKEND / "src")], capture_output=True, text=True,
                                 timeout=60, env={k: v for k, v in os.environ.items() if k not in ("KID_TE_ROOT", "LIVE_RE_ROOT")})
            operator_root = Path(out.stdout.strip().splitlines()[-1])
            from dotenv import dotenv_values
            for k, v in dotenv_values(operator_root / ".env").items():
                if k.startswith("DEEPSEEK_") and v and k not in os.environ:
                    os.environ[k] = v
        except Exception as exc:  # key stays unset; the responder then reports deepseek_key_missing
            print(f"warning: could not read operator model config: {type(exc).__name__}", file=sys.stderr)
    tmp = Path(tempfile.mkdtemp(prefix="kid-te-eval-"))
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)
    if "server" not in sys.modules:
        os.environ["KID_TE_ROOT"] = str(tmp / "runtime")
    return tmp


class _Catalog:
    def __init__(self):
        self.snapshot = {}

    def sync(self, snap):
        snap = snap or {}
        self.snapshot = {"room_id": snap.get("room_id", ""), "pin_id": snap.get("pin_id", ""),
                         "products": snap.get("products", []), "product_count": len(snap.get("products", [])), "synced_at": "now"}
        return self.snapshot


def build_engine(tmp, catalog, responder, tag):
    import server
    from knowledge import KnowledgeBase
    from customer_memory import CustomerMemory
    base = Path(tmp) / tag
    base.mkdir(parents=True, exist_ok=True)
    kb = {"nhom_san_pham": [{"san_pham": [{
        "san_pham_id": p["id"], "ten_san_pham": p["title"], "approved_for_live": True,
        "shop_product_ids": [p["tiktok_product_id"], p["shopee_product_id"]],
        "listing_aliases": p.get("aliases", ""), "huong_dan_su_dung": p["usage"],
        "cong_dung": p["uses"], "live_promotion": p.get("promotion", "")} for p in catalog["products"]]}]}
    (base / "knowledge.json").write_text(json.dumps(kb, ensure_ascii=False), encoding="utf-8")
    (base / "profiles.json").write_text('{"products":{}}', encoding="utf-8")
    memory = CustomerMemory(base / "memory.sqlite3", base / "journals", session_id="eval-" + tag, max_prior_turns=5)
    reporter = server.MissingReporter()
    reporter.path = base / "missing.jsonl"
    return server.ReplyEngine(responder=responder, memory=memory,
                              knowledge=KnowledgeBase(base / "knowledge.json", base / "profiles.json"),
                              catalog_store=_Catalog(), missing_reporter=reporter)


def _tiktok_products(catalog):
    return [{"product_id": p["tiktok_product_id"], "title": p["title"], "original_price": vnd(p["price"]),
             "sale_price": "", "promotion": "", "skus": []} for p in catalog["products"]]


def _shopee_state(catalog):
    products = [{"shop_id": p["shopee_shop_id"], "product_id": p["shopee_product_id"], "title": p["title"],
                 "price_data": {"currency": "VND", "price": str(p["price"]), "price_before_discount": str(p["price"])}}
                for p in catalog["products"]]
    mappings = {f"p{i}": {"shop_id": p["shopee_shop_id"], "product_id": p["shopee_product_id"]}
                for i, p in enumerate(catalog["products"])}
    return {"catalog": {"session_id": "123", "updated_at": time.time(), "products": products},
            "videos": {"v1": {"mappings": mappings, "alternates": {}}}}


def _observe(model):
    """Prices/dose rows the engine actually handed the model this turn (the verified facts)."""
    allowed, rows, price_asked = set(), [], False
    for entry in model.turn_log:
        ctx = entry["context"] if isinstance(entry["context"], dict) else {}
        price = ctx.get("price")
        if isinstance(price, dict) and price.get("quotable_price"):
            allowed.update(money_values(price["quotable_price"]))
            price_asked = True
        for row in ctx.get("verified_dose_calculations") or []:
            if isinstance(row, dict) and row not in rows:
                rows.append(row)
    return allowed, rows, price_asked


def replay_conversation(conv, catalog, model, tmp):
    platform, tag = conv["platform"], conv["id"]
    engine = build_engine(tmp, catalog, model, tag)
    shopee_engine = None
    if platform == "shopee":
        state = _shopee_state(catalog)
        engine.shopee_mapping_state = lambda: state
        shopee_engine = engine.shopee_ai()
    results = []
    first = catalog["products"][0]
    try:
        for i, turn in enumerate(conv["turns"], 1):
            model.start_turn()
            nick = "Khach" + tag.replace("-", "")
            cid = f"{tag}-{i}"
            try:
                if platform == "tiktok":
                    uid = "u" + tag.replace("-", "")
                    out = engine.handle({"comment": turn["comment"], "comment_id": cid, "user_id": uid,
                                         "unique_id": uid, "nickname": nick,
                                         "live_context": {"room_id": "room-" + tag, "pin_id": first["tiktok_product_id"],
                                                          "products": _tiktok_products(catalog)}})
                    tagtxt = "@" + uid
                else:
                    out = shopee_engine.reply({"session_id": "123", "video_id": "v1", "comment_id": cid,
                                               "user_id": "77" + str(zlib.crc32(tag.encode()) % 1000), "nickname": nick,
                                               "comment": turn["comment"], "shop_id": first["shopee_shop_id"],
                                               "product_id": first["shopee_product_id"], "title": first["title"]})
                    tagtxt = "@" + nick
            except CallBudget:
                raise
            except Exception as exc:  # an engine error is a failed turn, not a crashed run
                out, tagtxt = {"ok": False, "should_send": False, "reason": f"exception:{type(exc).__name__}"}, ""
            bodies = []
            if out.get("should_send"):
                bodies = list(out.get("messages") or [out.get("reply", "")])
                bodies = [b[len(tagtxt) + 1:] if b.startswith(tagtxt + " ") else b for b in bodies]
                try:  # mimic the extension receipt so the next turn sees this reply as history
                    if platform == "tiktok":
                        engine.acknowledge_reply({"interaction_id": out.get("interaction_id"), "reply_id": out.get("reply_id"),
                                                  "receipt": {"room_id": "room-" + tag, "response_code": 0}})
                    else:
                        shopee_engine.delivery({"session_id": "123", "turn_id": out.get("turn_id"), "message_id": "m" + cid})
                except Exception:
                    pass
            allowed, rows, price_asked = _observe(model)
            observed = {"sent": bool(bodies), "bodies": bodies, "tag": tagtxt, "allowed_prices": allowed,
                        "dose_rows": rows, "price_asked": price_asked, "reason": out.get("reason", "")}
            scored = score_turn({**turn, "comment": turn["comment"]}, observed, catalog)
            results.append({"turn": i, "comment": turn["comment"], "intent": turn["expect"]["intent"],
                            "sent": bool(bodies), "reason": out.get("reason", ""), "reply": " | ".join(bodies), **scored})
    finally:
        try:
            engine.close()
        except Exception:
            pass
    return results


def load_fixtures():
    ids = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))["ids"]
    return [json.loads((FIXTURES / f"{i}.json").read_text(encoding="utf-8")) for i in ids]


def estimate_calls(fixtures):
    return sum(len(c["turns"]) for c in fixtures) * 3 // 2 + 1


def evaluate(model, *, fixtures=None, catalog=None, tmp=None, max_calls=None, real=False):
    fixtures = fixtures or load_fixtures()
    catalog = catalog or load_catalog()
    tmp = Path(tmp or _seed_root_env(real)) / ("run-%d" % time.time_ns())
    wrapped = RecordingModel(model, max_calls)
    convs, aborted = [], ""
    for conv in fixtures:
        try:
            convs.append({"id": conv["id"], "platform": conv["platform"], "turns": replay_conversation(conv, catalog, wrapped, tmp)})
        except CallBudget as exc:
            aborted = str(exc)
            break
    turns = [t for c in convs for t in c["turns"]]
    passed = sum(1 for t in turns if t["passed"])
    severe = [{"conversation": c["id"], "turn": t["turn"], "errors": t["severe"]} for c in convs for t in c["turns"] if t["severe"]]
    rate = passed / len(turns) if turns else 0.0
    return {"date": datetime.date.today().isoformat(), "mode": "real" if real else "fake", "calls": wrapped.calls,
            "aborted": aborted, "turns": len(turns), "passed_turns": passed, "pass_rate": round(rate, 4),
            "severe_count": len(severe), "severe": severe,
            "overall_pass": bool(turns) and not aborted and rate >= PASS_RATE and not severe,
            "conversations": convs}


def render_md(report):
    lines = [f"# Conversation eval {report['date']} ({report['mode']})", "",
             f"- Result: **{'PASS' if report['overall_pass'] else 'FAIL'}** (need >= {int(PASS_RATE * 100)}% turns and 0 severe)",
             f"- Turns: {report['passed_turns']}/{report['turns']} ({report['pass_rate'] * 100:.1f}%)",
             f"- Severe errors: {report['severe_count']}", f"- Model calls: {report['calls']}"]
    if report["aborted"]:
        lines.append(f"- ABORTED: {report['aborted']}")
    lines += ["", "## Failing turns", ""]
    bad = [(c["id"], t) for c in report["conversations"] for t in c["turns"] if not t["passed"]]
    if not bad:
        lines.append("none")
    for cid, t in bad:
        lines.append(f"- {cid} turn {t['turn']} [{t['intent']}] `{t['comment']}`: severe={t['severe']} problems={t['problems']} reply=`{t['reply']}`")
    return "\n".join(lines) + "\n"


def write_reports(report, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{report['date']}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / f"{report['date']}.md").write_text(render_md(report), encoding="utf-8")
    return out / f"{report['date']}.json", out / f"{report['date']}.md"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--real", action="store_true", help="use the configured real model (costs money)")
    ap.add_argument("--yes", action="store_true", help="confirm the real-model run")
    ap.add_argument("--max-calls", type=int, default=120)
    ap.add_argument("--out", default=str(REPO / "test-artifacts" / "eval"))
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    fixtures = load_fixtures()
    if args.real:
        print(f"Estimated model calls: ~{estimate_calls(fixtures)} (cap --max-calls {args.max_calls}; "
              f"{sum(len(c['turns']) for c in fixtures)} turns)")
        if not args.yes:
            print("Not calling the model. Re-run with --yes to proceed.")
            return 2
    tmp = _seed_root_env(args.real)
    if args.real:
        import server
        model = server.DeepSeekResponder()
    else:
        model = FakeModel()
    report = evaluate(model, fixtures=fixtures, tmp=tmp, max_calls=args.max_calls if args.real else None, real=args.real)
    js, md = write_reports(report, args.out)
    print(render_md(report))
    print(f"Wrote {js}\nWrote {md}")
    return 0 if report["overall_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
