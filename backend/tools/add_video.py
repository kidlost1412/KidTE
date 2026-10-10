"""Them mot video phat lai moi: mot lenh ra ca van tay va lich nhap.

    python tools/add_video.py --id live2 --video "D:/.../live2.mp4"
    python tools/add_video.py --id live2 --video "..." --transcript "...json"
    python tools/add_video.py --id live1 --audio "...audio.wav" --only-fingerprint

Lich sinh ra la BAN NHAP de nguoi van hanh soat: script chi doan doan nao dang
noi san pham nao dua theo tu khoa trong ban chep loi. Doc xong phai mo
data/videos/<id>/timeline.json sua lai mot luot.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from console_io import force_utf8_console  # noqa: E402

force_utf8_console()

from video_fingerprint import (  # noqa: E402
    TARGET_RATE, read_pcm16, resample_mono, save_reference, spectral_features,
)
from video_library import VIDEO_DIR, validate_timeline  # noqa: E402

# Tu khoa nhan dien san pham trong ban chep loi. Nhan dang giong noi hay sai
# chinh ta ten hoat chat nen de nhieu bien the.
PRODUCT_HINTS = {
    "tc01": {
        "bag_index": 1, "short_name": "TC01", "title": "Kích Rễ Cực Mạnh TC01",
        "include_all": ["tc01"], "exclude_any": [],
        "keywords": ["tc01", "tc 01", "kich re", "kích rễ", "giam canh", "giâm cành"],
    },
    "fish1": {
        "bag_index": 2, "short_name": "Đạm cá 1L",
        "title": "Đạm Cá Amino 1L Chất Hữu Cơ 20%",
        "include_all": ["dam ca", "1l"], "exclude_any": ["5 lit", "5l", "can 5"],
        "keywords": ["dam ca", "đạm cá", "amino", "chai 1 lit", "chai 1l"],
    },
    "topone": {
        "bag_index": 3, "short_name": "Top Bo One",
        "title": "BO TRI ONE | Chế Phẩm Top Bo One Tri",
        "include_all": ["bo tri one"], "exclude_any": [],
        "keywords": ["top one", "top bo one", "bo tri", "bọ trĩ", "ray", "rầy",
                     "rep sap", "rệp sáp", "chich hut", "chích hút", "nhen do"],
    },
    "fish5": {
        "bag_index": 4, "short_name": "Đạm cá 5L",
        "title": "Đạm Cá Hữu Cơ Amino Giúp Cây Bật Mầm (Can 5 Lít)",
        "include_all": ["dam ca", "5 lit"], "exclude_any": [],
        "keywords": ["can 5", "5 lit", "5 lít", "5l"],
    },
    "soil": {
        "bag_index": 5, "short_name": "Sạch Sâu Đất",
        "title": "SẠCH SÂU ĐẤT 1KG Công Nghệ",
        "include_all": ["sach sau dat", "1kg"], "exclude_any": [],
        "keywords": ["sach sau dat", "sạch sâu đất", "sau dat", "sâu đất",
                     "tuyen trung", "tuyến trùng"],
    },
}
MIN_SEGMENT_MS = 25000   # doan ngan hon nay thi gop vao doan truoc


def fold(value):
    text = unicodedata.normalize("NFD", str(value or "").casefold().replace("đ", "d"))
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def extract_audio(video):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("Không tìm thấy ffmpeg trong PATH")
    out = Path(tempfile.gettempdir()) / f"kid_te_ref_{Path(video).stem}_16k.wav"
    print(f"Đang rút âm thanh bằng ffmpeg -> {out.name}")
    subprocess.run(
        [ffmpeg, "-y", "-i", str(video), "-vn", "-ac", "1",
         "-ar", str(TARGET_RATE), "-acodec", "pcm_s16le", str(out)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return out


def build_fingerprint(source, out_path):
    samples, rate = read_pcm16(source)
    samples, rate = resample_mono(samples, rate)
    duration = len(samples) / rate
    features = spectral_features(samples, rate)
    save_reference(out_path, features, duration, source)
    print(f"Vân tay: {features.shape[0]} khung · {duration:.2f}s"
          f" ({duration/60:.2f} phút) -> {out_path.name}")
    return duration


def score_segments(segments):
    """Gan diem tung san pham cho tung doan chep loi."""
    rows = []
    for item in segments:
        text = fold(item.get("text"))
        hits = {}
        for key, spec in PRODUCT_HINTS.items():
            count = sum(text.count(fold(word)) for word in spec["keywords"])
            if count:
                hits[key] = count
        rows.append({
            "start_ms": int(float(item.get("start", 0)) * 1000),
            "end_ms": int(float(item.get("end", 0)) * 1000),
            "text": str(item.get("text") or "").strip(),
            "hits": hits,
        })
    return rows


def propose_events(rows, cycle_ms):
    """Gop cac doan lien tiep cung san pham thanh mot moc ghim."""
    current = None
    blocks = []
    for row in rows:
        key = max(row["hits"], key=row["hits"].get) if row["hits"] else None
        if key is None:
            continue
        if current and current["product"] == key:
            current["end_ms"] = row["end_ms"]
            current["evidence"].append(row["text"])
        else:
            if current:
                blocks.append(current)
            current = {"product": key, "at_ms": row["start_ms"],
                       "end_ms": row["end_ms"], "evidence": [row["text"]]}
    if current:
        blocks.append(current)

    merged = []
    for block in blocks:
        if merged and block["at_ms"] - merged[-1]["end_ms"] < MIN_SEGMENT_MS \
                and merged[-1]["product"] == block["product"]:
            merged[-1]["end_ms"] = block["end_ms"]
            merged[-1]["evidence"] += block["evidence"]
            continue
        if block["end_ms"] - block["at_ms"] < MIN_SEGMENT_MS and merged:
            merged[-1]["end_ms"] = block["end_ms"]
            continue
        merged.append(block)

    events, contexts = [], []
    for block in merged:
        at = min(block["at_ms"], cycle_ms - 1000)
        if events and at <= events[-1]["at_ms"]:
            continue
        # Ghim lai dung san pham dang ghim la vo ich va lam the san pham nhay
        # truoc mat nguoi xem.
        if events and events[-1]["product"] == block["product"]:
            continue
        events.append({"at_ms": at, "product": block["product"]})
        sample = " ".join(block["evidence"])[:220]
        contexts.append({
            "at_ms": at,
            "topic": f"{PRODUCT_HINTS[block['product']]['short_name']}"
                     f" (tự dò từ bản chép lời — SOÁT LẠI)",
            "facts": [],
            "_trich_ban_chep_loi": sample,
        })
    if not events:
        events.append({"at_ms": 0, "product": next(iter(PRODUCT_HINTS))})
        contexts.append({"at_ms": 0, "topic": "Đầu vòng — SOÁT LẠI", "facts": []})
    elif events[0]["at_ms"] != 0:
        # Keo moc dau tien ve 0 thay vi chen them mot moc trung san pham: chen
        # them se sinh ra hai moc lien tiep cung mot san pham.
        events[0]["at_ms"] = 0
        if contexts:
            contexts[0]["at_ms"] = 0
    return events, contexts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True, help="ma video, vd live2")
    parser.add_argument("--video", help="duong dan file .mp4")
    parser.add_argument("--audio", help="duong dan .wav neu da co san")
    parser.add_argument("--transcript", help="transcript JSON {segments:[{start,end,text}]}")
    parser.add_argument("--title", default="")
    parser.add_argument("--only-fingerprint", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="ghi de timeline.json da co")
    args = parser.parse_args()

    video_id = re.sub(r"[^A-Za-z0-9_-]", "", args.id)
    if not video_id:
        raise SystemExit("--id chỉ dùng chữ, số, gạch ngang, gạch dưới")
    if not args.video and not args.audio:
        raise SystemExit("Cần --video hoặc --audio")

    folder = VIDEO_DIR / video_id
    folder.mkdir(parents=True, exist_ok=True)
    source = Path(args.audio) if args.audio else extract_audio(args.video)
    duration = build_fingerprint(source, folder / "fingerprint.npz")
    cycle_ms = int(round(duration * 1000))

    timeline_path = folder / "timeline.json"
    if args.only_fingerprint:
        if timeline_path.exists():
            existing = json.loads(timeline_path.read_text(encoding="utf-8-sig"))
            if existing.get("cycle_ms") != cycle_ms:
                print(f"\n*** cycle_ms trong timeline.json là"
                      f" {existing.get('cycle_ms')} ms nhưng video dài"
                      f" {cycle_ms} ms — phải sửa lại ***")
            else:
                print("cycle_ms trong timeline.json khớp độ dài video.")
        print("Xong (chỉ dựng vân tay).")
        return 0
    if timeline_path.exists() and not args.force:
        raise SystemExit(f"{timeline_path} đã có. Thêm --force nếu muốn ghi đè.")

    events, contexts = [{"at_ms": 0, "product": "tc01"}], [
        {"at_ms": 0, "topic": "SOÁT LẠI: chưa có bản chép lời", "facts": []}]
    if args.transcript:
        data = json.loads(Path(args.transcript).read_text(encoding="utf-8-sig"))
        segments = data.get("segments") if isinstance(data, dict) else data
        rows = score_segments(segments or [])
        found = sum(1 for row in rows if row["hits"])
        print(f"Bản chép lời: {len(rows)} đoạn, {found} đoạn có nhắc sản phẩm")
        events, contexts = propose_events(rows, cycle_ms)

    timeline = {
        "schema_version": 1,
        "video_id": video_id,
        "title": args.title or f"Video {video_id}",
        "source_file": Path(args.video or args.audio).name,
        "cycle_ms": cycle_ms,
        "repin_every_ms": 20000,
        "_huong_dan": "BAN NHAP do may de xuat. Soat lai events va"
                      " script_contexts truoc khi len live.",
        "products": {key: {k: v for k, v in spec.items() if k != "keywords"}
                     for key, spec in PRODUCT_HINTS.items()},
        "events": events,
        "script_contexts": contexts,
    }
    errors = validate_timeline(timeline)
    timeline_path.write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nĐã ghi {timeline_path}")
    print(f"  cycle_ms = {cycle_ms} ms ({duration/60:.2f} phút)")
    print(f"  {len(events)} mốc ghim đề xuất:")
    for event in events:
        minutes, seconds = divmod(event["at_ms"] // 1000, 60)
        print(f"    {minutes:02d}:{seconds:02d}  {event['product']}")
    used = {event["product"] for event in events if event.get("product")}
    missing = [key for key in PRODUCT_HINTS if key not in used]
    if missing:
        print(f"\n  Chưa mốc nào dùng: {', '.join(missing)}"
              " — nếu video có giới thiệu thì phải thêm mốc bằng tay.")
    if errors:
        print("\n  *** Lịch còn lỗi, phải sửa trước khi dùng: ***")
        for error in errors:
            print(f"    - {error}")
        return 1
    print("\nLịch hợp lệ. MỞ FILE RA SOÁT rồi khởi động lại backend.")
    print("Kiểm nhanh: .venv\\Scripts\\python.exe tools\\check_setup.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
