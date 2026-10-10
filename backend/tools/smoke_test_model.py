"""Goi THAT mot luot den DeepSeek de chac model va key dung.

/health hien chi kiem SO DU, khong kiem ten model. Neu DEEPSEEK_MODEL sai ten
thi AI van bao "san sang" roi im lang o moi binh luan voi
missing_fields=[deepseek_NotFoundError]. Day la kiem tra con thieu.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from console_io import force_utf8_console  # noqa: E402

force_utf8_console()

from config import (  # noqa: E402
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_DISABLE_THINKING,
    DEEPSEEK_MODEL, DEEPSEEK_REASONING_EFFORT,
)

print(f"base_url : {DEEPSEEK_BASE_URL}")
print(f"model    : {DEEPSEEK_MODEL}")
print(f"thinking : {'tắt' if DEEPSEEK_DISABLE_THINKING else 'bật, effort=' + DEEPSEEK_REASONING_EFFORT}")
print(f"key      : {'có' if DEEPSEEK_API_KEY else 'THIẾU'}\n")

if not DEEPSEEK_API_KEY:
    raise SystemExit("Thiếu DEEPSEEK_API_KEY trong .env")

from openai import OpenAI  # noqa: E402

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL,
                timeout=60, max_retries=0)

print("Danh sách model tài khoản này được dùng:")
try:
    available = [item.id for item in client.models.list().data]
    for name in available:
        mark = "  <-- đang cấu hình" if name == DEEPSEEK_MODEL else ""
        print(f"  {name}{mark}")
    if DEEPSEEK_MODEL not in available:
        print(f"\n  *** '{DEEPSEEK_MODEL}' KHÔNG có trong danh sách trên ***")
except Exception as exc:
    print(f"  không liệt kê được: {type(exc).__name__}: {exc}")

print("\nGọi thật một lượt (giống hệt lúc trả lời bình luận):")
inference = {
    "model": DEEPSEEK_MODEL,
    "messages": [
        {"role": "system", "content":
         'Trả đúng một JSON object: {"action":"reply","reply":"..."}'},
        {"role": "user", "content": 'Khách hỏi: "giá bao nhiêu". '
         'Giá được phép nói: 120.000₫. Trả lời ngắn, dưới 80 ký tự.'},
    ],
    "max_tokens": 2400,
    "response_format": {"type": "json_object"},
    "extra_body": {"thinking": {
        "type": "disabled" if DEEPSEEK_DISABLE_THINKING else "enabled"}},
}
if not DEEPSEEK_DISABLE_THINKING:
    inference["reasoning_effort"] = DEEPSEEK_REASONING_EFFORT

started = time.monotonic()
try:
    response = client.chat.completions.create(**inference)
except Exception as exc:
    print(f"  THẤT BẠI: {type(exc).__name__}: {exc}")
    raise SystemExit(1) from None
elapsed = (time.monotonic() - started) * 1000
choice = response.choices[0]
usage = response.usage
details = getattr(usage, "completion_tokens_details", None)
print(f"  độ trễ        : {elapsed:.0f} ms")
print(f"  finish_reason : {choice.finish_reason}")
print(f"  token vào/ra  : {usage.prompt_tokens} / {usage.completion_tokens}")
print(f"  token suy luận: {getattr(details, 'reasoning_tokens', 0) or 0}")
print(f"  nội dung      : {(choice.message.content or '')[:200]!r}")
try:
    parsed = json.loads(choice.message.content or "")
    print(f"  JSON hợp lệ   : có · action={parsed.get('action')}")
except json.JSONDecodeError:
    print("  JSON hợp lệ   : KHÔNG — model không trả JSON đúng khuôn")
    raise SystemExit(1) from None
print("\nMô hình và key chạy được.")
