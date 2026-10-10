"""Generate Sakura tokens from one palette for Kid TE and both extension UIs."""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START = "/* SAKURA GENERATED START */"
END = "/* SAKURA GENERATED END */"


def declarations(values, aliases=False):
    out = [f"--{key}: {value};" for key, value in values.items()]
    if aliases:
        out += ["--ink: var(--text);", "--muted: var(--text-3);", "--line: var(--border);", "--soft: var(--brand-soft);"]
    return "\n  " + "\n  ".join(out) + "\n"


def outputs():
    palette = json.loads((ROOT / "ui/theme/sakura.json").read_text(encoding="utf-8"))
    for target, host, aliases in [
        ("backend/src/web/styles/tokens.css", ":root", False),
        ("ui/extension/popup.css", ":root", True),
        ("ui/extension/live-widget.js", ":host", True),
    ]:
        light = declarations(palette["light"], aliases)
        dark = declarations(palette["dark"], aliases)
        block = f"{START}\n{host} {{\n  color-scheme: light;{light}}}\n"
        if host == ":root" and not aliases:
            block += f':root[data-theme="dark"] {{\n  color-scheme: dark;{dark}}}\n'
            selector = ':root:not([data-theme="light"])'
        else:
            selector = host
        block += f"@media (prefers-color-scheme: dark) {{ {selector} {{\n  color-scheme: dark;{dark}}} }}\n{END}"
        path = ROOT / target
        original = path.read_text(encoding="utf-8")
        if START in original:
            before, rest = original.split(START, 1)
            _, after = rest.split(END, 1)
            updated = before + block + after
        elif host == ":host":
            updated = original.replace(':host{all:initial}', block + '\n:host{all:initial}', 1)
        else:
            updated = original.rstrip() + "\n\n" + block + "\n"
        if host == ":root":
            before, rest = updated.split(START,1)
            keys = list(palette["light"]) + (["ink","muted","line","soft"] if aliases else [])
            before = re.sub(r"--(?:" + "|".join(map(re.escape,keys)) + r")\s*:[^;{}]+;", "", before)
            before = before.replace('Thương hiệu: xanh lục đậm (nông nghiệp) — dùng tiết chế', 'Thương hiệu và màu ngữ nghĩa được sinh từ ui/theme/sakura.json ở cuối file')
            before = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", before)
            updated = before + START + rest
        yield path, original, updated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for path, original, updated in outputs():
        if args.check and original != updated:
            raise SystemExit(f"Theme needs generation: {path}")
        if not args.check:
            path.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    main()
