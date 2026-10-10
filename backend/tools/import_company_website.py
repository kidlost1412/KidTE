"""Lấy và chuẩn hóa toàn bộ trang sản phẩm từ website chính thức của công ty.

Script này không tự ghi vào approved_product_knowledge.json. Nó tạo snapshot
nguồn, hồ sơ đã tách mục và báo cáo ứng viên ghép với catalog LIVE để người
triển khai chỉ duyệt các cặp thật sự cùng sản phẩm/quy cách.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import unicodedata
import urllib.request
from datetime import datetime
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API = "https://nongduoctoancaugroup.com/wp-json/wp/v2/product"
SITE = "https://nongduoctoancaugroup.com"
CONTACT_MARKERS = (
    "nông dược toàn cầu – đi đầu", "nong duoc toan cau - di dau",
    "112 nguyễn huệ", "112 nguyen hue", "09 1819 2425",
    "nongduoctoancaugroup.com",
)
SECTION_NAMES = {
    "thanh phan": "ingredients",
    "cong dung": "uses",
    "uu diem noi bat": "advantages",
    "huong dan su dung": "directions",
    "luu y": "notes",
}
STOP = {
    "san", "pham", "phan", "bon", "nong", "duoc", "toan", "cau", "giup",
    "cay", "trong", "chai", "goi", "can", "huu", "co", "dac", "tri",
    "sieu", "cuc", "manh", "cong", "nghe", "moi", "dang", "cho", "va",
    "cac", "loai", "thuoc", "npk", "the", "he", "chuyen", "dung",
}
PACKAGE_RE = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(ml|lít|lit|l|kg|gr|g)(?![a-z])", re.I)


def fold(value):
    text = unicodedata.normalize("NFD", html.unescape(str(value or "")).casefold())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text.replace("đ", "d")).strip()


def tokens(value):
    return {item for item in fold(value).split() if len(item) > 1 and item not in STOP}


def packages(value):
    result = set()
    for amount, unit in PACKAGE_RE.findall(fold(value)):
        unit = {"l": "lit", "lít": "lit", "g": "gr"}.get(unit.casefold(), unit.casefold())
        amount = amount.replace(",", ".")
        if "." in amount:
            amount = amount.rstrip("0").rstrip(".")
        result.add(f"{amount}{unit}")
    return sorted(result)


class TextExtractor(HTMLParser):
    BLOCKS = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr", "td"}

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def text(self):
        raw = html.unescape("".join(self.parts)).replace("\xa0", " ")
        lines = []
        for line in raw.splitlines():
            line = re.sub(r"\s+", " ", line).strip(" \t•●+–—-")
            if not line:
                continue
            folded = fold(line)
            if any(fold(marker) in folded for marker in CONTACT_MARKERS):
                continue
            if line not in lines:
                lines.append(line)
        return "\n".join(lines)


def split_sections(text):
    sections = {key: [] for key in SECTION_NAMES.values()}
    preamble = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        folded_line = fold(line)
        matched = None
        remainder = ""
        for label, name in SECTION_NAMES.items():
            if folded_line == label:
                matched = name
                break
            # Nhiều trang viết "Hướng dẫn sử dụng: 50 ml..." trên cùng
            # một dòng. Chỉ nhận dấu hai chấm để tránh cắt nhầm câu văn.
            heading, separator, value = line.partition(":")
            if separator and fold(heading) == label and value.strip():
                matched = name
                remainder = value.strip()
                break
        if matched:
            current = matched
            if remainder:
                sections[current].append(remainder)
            continue
        if current:
            sections[current].append(line)
        else:
            preamble.append(line)
    return {"preamble": preamble, **sections}


def fetch_products():
    url = (API + "?per_page=100&_embed=1&"
           "_fields=id,slug,link,title,content,excerpt,date,modified,"
           "featured_media,categories,_links,_embedded")
    request = urllib.request.Request(url, headers={"User-Agent": "Kid-TE-Catalog/1.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        products = json.load(response)
        total = int(response.headers.get("X-WP-Total", len(products)))
    if len(products) != total:
        raise RuntimeError(f"API trả {len(products)}/{total} sản phẩm")
    return products


def normalize_product(product):
    parser = TextExtractor()
    parser.feed(product.get("content", {}).get("rendered", ""))
    body = parser.text()
    title = html.unescape(product.get("title", {}).get("rendered", "")).strip()
    sections = split_sections(body)
    embedded_media = (product.get("_embedded") or {}).get("wp:featuredmedia") or []
    featured = embedded_media[0] if embedded_media else {}
    term_groups = (product.get("_embedded") or {}).get("wp:term") or []
    categories = sorted({
        term.get("name", "").strip()
        for group in term_groups if isinstance(group, list)
        for term in group if isinstance(term, dict)
        and term.get("taxonomy") == "product_cat" and term.get("name", "").strip()
    })
    return {
        "website_product_id": str(product.get("id", "")),
        "name": title,
        "slug": product.get("slug", ""),
        "url": product.get("link", ""),
        "source_modified": product.get("modified", ""),
        "featured_media_id": product.get("featured_media") or 0,
        "featured_image_url": featured.get("source_url", ""),
        "featured_image_alt": featured.get("alt_text", ""),
        "categories": categories,
        "packages": packages(title),
        "ingredients": sections["ingredients"],
        "uses": sections["uses"],
        "advantages": sections["advantages"],
        "directions": sections["directions"],
        "notes": sections["notes"],
        "preamble": sections["preamble"],
        "source_text": body,
    }


def match_score(live, web):
    left = tokens(live.get("title"))
    right = tokens(web.get("name")) | tokens(web.get("slug"))
    overlap = left & right
    union = left | right
    jaccard = len(overlap) / max(1, len(union))
    containment = len(overlap) / max(1, min(len(left), len(right)))
    sequence = SequenceMatcher(None, fold(live.get("title"))[:180], fold(web.get("name"))).ratio()
    lp = set(packages(live.get("title")))
    wp = set(web.get("packages") or [])
    package_state = "unknown"
    package_adjust = 0.0
    if lp and wp:
        package_state = "match" if lp & wp else "conflict"
        package_adjust = 0.25 if package_state == "match" else -0.45
    score = jaccard * 0.45 + containment * 0.35 + sequence * 0.20 + package_adjust
    return round(score, 4), sorted(overlap), package_state


def build_match_report(live_products, profiles):
    report = []
    for live in live_products:
        ranked = []
        for web in profiles:
            score, overlap, package_state = match_score(live, web)
            ranked.append({
                "website_product_id": web["website_product_id"],
                "name": web["name"],
                "url": web["url"],
                "score": score,
                "overlap": overlap,
                "package_state": package_state,
            })
        ranked.sort(key=lambda item: item["score"], reverse=True)
        best = ranked[0]
        margin = round(best["score"] - ranked[1]["score"], 4)
        auto_safe = (
            best["score"] >= 0.62 and margin >= 0.12 and
            best["package_state"] != "conflict"
        )
        report.append({
            "shop_product_id": str(live.get("product_id", "")),
            "live_title": live.get("title", ""),
            "live_packages": packages(live.get("title")),
            "auto_safe": auto_safe,
            "margin": margin,
            "candidates": ranked[:3],
        })
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().astimezone().date().isoformat())
    args = parser.parse_args()
    source_dir = ROOT / "data" / "sources"
    source_dir.mkdir(parents=True, exist_ok=True)
    raw = fetch_products()
    profiles = [normalize_product(item) for item in raw]
    live = json.loads((ROOT / "data" / "live_products_current.json").read_text(encoding="utf-8"))
    match_report = build_match_report(live.get("products", []), profiles)

    outputs = {
        source_dir / f"company_website_products_{args.date}.json": {
            "schema_version": 1,
            "source": API,
            "site": SITE,
            "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "product_count": len(raw),
            "products": raw,
        },
        source_dir / f"company_website_profiles_{args.date}.json": {
            "schema_version": 1,
            "source": API,
            "product_count": len(profiles),
            "products": profiles,
        },
        source_dir / f"company_website_live_match_{args.date}.json": {
            "schema_version": 1,
            "source": API,
            "live_product_count": len(live.get("products", [])),
            "website_product_count": len(profiles),
            "auto_safe_count": sum(1 for item in match_report if item["auto_safe"]),
            "matches": match_report,
        },
    }
    for path, data in outputs.items():
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
        print(path)
    print(f"website={len(profiles)} live={len(live.get('products', []))} "
          f"auto_safe={sum(1 for item in match_report if item['auto_safe'])}")


if __name__ == "__main__":
    main()
