"""Bộ xử lý liều thuần; không I/O, không import plugin khác."""
from __future__ import annotations

import re

from knowledge import clean


# Người trồng nói "bình 16 lít", "phuy 200 lít", "can 20l", hoặc chỉ "20 lít
# nước". Bản cũ chỉ bắt "bình N lít" nên TC01 — sản phẩm ghim nhiều nhất, có
# hướng dẫn ghi "cho bình 16 lít" — không bao giờ quy đổi được liều.
_TANK_WORDS = r"(?:bình|binh|phuy|thùng|thung|bồn|bon)"
_TARGET_TANK = re.compile(
    rf"({_TANK_WORDS})?\s*(\d+(?:[.,]\d+)?)\s*l(?:ít|it)?(?![a-zA-Z0-9])", re.I,
)
# Hướng dẫn hay ghi một KHOẢNG ("30–50ml cho 20 lít"). Lấy mỗi cận trên là
# đẩy khách về phía liều cao nhất — giữ cả hai đầu để nói đúng như nhãn.
# Nhan san pham viet lieu theo nhieu kieu:
#   "Pha 240ml cho phuy 200 lit"            -> mot ty le
#   "30-50ml cho 20 lit"                    -> khoang o phia ml
#   "Pha 20ml voi binh 16 - 25 lit nuoc"    -> khoang o phia LIT
#   "chai 100ml pha duoc 400 - 800 lit"     -> khoang o phia LIT
# Ban dau khong bat duoc hai kieu cuoi nen phan lon lieu tu website thanh vo
# dung. Nhom bat ca hai phia.
#   "Cho 50gr vao binh 16 - 30 lit"         -> lieu dang BOT, don vi gam
#   "Binh 16 lit: 25 - 30ml"                -> viet NGUOC, binh truoc lieu sau
# Hai kieu cuoi truoc day khong bat duoc: ca combo Nano Tonic + A6 va moi
# san pham dang bot (Rydoxin 30g, Jialeton 35g, A6 50gr, Magie Kem 50gr...)
# deu khong co phep tinh nao, tuc la model tu nham lieu thuoc.
_DOSE_UNIT = r"(ml|kg|gr(?:am)?|g)(?![a-zA-Z])"
_LITRE_SIDE = (r"(\d+(?:[.,]\d+)?)"
               r"(?:\s*[-–—]\s*(\d+(?:[.,]\d+)?))?\s*l(?:ít|it)?(?![a-zA-Z0-9])")
_AMOUNT_SIDE = (r"(\d+(?:[.,]\d+)?)"
                rf"(?:\s*[-–—]\s*(\d+(?:[.,]\d+)?))?\s*{_DOSE_UNIT}")
_SOURCE_RATIO = re.compile(
    rf"{_AMOUNT_SIDE}[^\d]{{0,24}}?(?:{_TANK_WORDS}\s*)?{_LITRE_SIDE}", re.I)
# Kieu nguoc BAT BUOC phai co tu chi binh, neu khong thi "chai 100ml pha 400
# lit" se bi doc nguoc thanh "100 lit cho 400ml".
_SOURCE_RATIO_REVERSED = re.compile(
    rf"{_TANK_WORDS}\s*{_LITRE_SIDE}[^\d]{{0,12}}?{_AMOUNT_SIDE}", re.I)

SETTINGS_SCHEMA = [
    {"key": "tinh_lieu", "type": "bool", "default": True, "group": "Tính liều",
     "label": "Tự quy đổi liều theo cỡ bình khách hỏi",
     "help": "Tắt thì AI chỉ đọc nguyên văn hướng dẫn trên nhãn, không tự nhân chia."},
    {"key": "quy_doi_ngoai_khoang", "type": "bool", "default": True, "group": "Tính liều",
     "label": "Cho phép quy đổi khi bình khách hỏi nằm ngoài khoảng ghi trên nhãn",
     "help": "Chỉ ngoại suy khi hồ sơ của chính sản phẩm có dose_extrapolation_rule đã duyệt."},
]
SETTINGS = {field["key"]: field["default"] for field in SETTINGS_SCHEMA}


def configure(values):
    SETTINGS.update(values or {})


def target_tank_liters(comment):
    """Cỡ bình khách hỏi, không phải dung tích chai khách đang cầm.

    "đạm cá 1L pha cho bình 20 lít" có hai số. Bản đầu lấy số đầu tiên nên ra
    liều cho bình 1 lít — sai gấp 20 lần. Ưu tiên số đi kèm từ chỉ bình; không
    có thì lấy số cuối, vì người ta nói tên sản phẩm trước, cỡ bình sau.
    """
    matches = list(_TARGET_TANK.finditer(clean(comment, 500)))
    if not matches:
        return 0.0
    explicit = [item for item in matches if item.group(1)]
    chosen = explicit[0] if explicit else matches[-1]
    try:
        return float(chosen.group(2).replace(",", "."))
    except ValueError:
        return 0.0


def parse_dose_line(source):
    """Doc mot dong huong dan -> (low, high, don_vi, lit_thap, lit_cao)."""
    match = _SOURCE_RATIO.search(source)
    if match:
        low, high, unit, litre_low, litre_high = match.groups()
    else:
        match = _SOURCE_RATIO_REVERSED.search(source)
        if not match:
            return None
        litre_low, litre_high, low, high, unit = match.groups()
    unit = {"gram": "g", "gr": "g"}.get((unit or "ml").lower(), (unit or "ml").lower())
    try:
        low_value = float(low.replace(",", "."))
        high_value = float((high or low).replace(",", "."))
        litre_low_value = float(litre_low.replace(",", "."))
        litre_high_value = float((litre_high or litre_low).replace(",", "."))
    except (AttributeError, ValueError):
        return None
    if low_value > high_value:
        low_value, high_value = high_value, low_value
    # kg -> g truoc khi tinh. "1kg pha 1500 lit" ma de nguyen kg thi binh 20L
    # ra 0.013kg, lam tron 2 so con 0.01kg — sai 25% VA khong ai do duoc
    # 0,01kg ngoai vuon. Doi sang gam thi ra 13g, dung va dung duoc.
    if unit == "kg":
        low_value, high_value, unit = low_value * 1000, high_value * 1000, "g"
    return low_value, high_value, unit, litre_low_value, litre_high_value


def _has_multiple_ratios(source):
    """True nếu dòng chứa từ hai tỷ lệ liều trở lên (không thể biết cái nào đúng)."""
    spans = []
    for regex in (_SOURCE_RATIO, _SOURCE_RATIO_REVERSED):
        for match in regex.finditer(source):
            if not any(match.start() < end and start < match.end() for start, end in spans):
                spans.append((match.start(), match.end()))
    if len(spans) <= 1:
        return False
    # Cùng nồng độ (lệch ≤10%, cùng đơn vị) chỉ là ghi cho nhiều cỡ bình: không mâu thuẫn.
    ratios = []
    for start, end in spans:
        parsed = parse_dose_line(source[start:end])
        if not parsed or parsed[3] <= 0 or parsed[4] <= 0:
            return True
        low, high, unit, litre_low, litre_high = parsed
        ratios.append((unit, ((low + high) / 2) / ((litre_low + litre_high) / 2)))
    units = {unit for unit, _ in ratios}
    values = [value for _, value in ratios]
    return len(units) > 1 or max(values) > min(values) * 1.1


def approved_directions(approved):
    """Gộp hướng dẫn ở bản ghi và ở hồ sơ duyệt (kể cả phần kế thừa).

    Chai 1L ghi "dùng theo cùng công thức 5L" nên nếu chỉ đọc bản ghi thì
    không có tỷ lệ nào để quy đổi, dù hồ sơ duyệt có đủ số theo nhãn.
    """
    approved = approved or {}
    found = []
    for source in (approved.get("huong_dan_su_dung"),
                   (approved.get("ho_so_da_duyet") or {}).get("directions")):
        if isinstance(source, str):
            found.append(source)
        elif isinstance(source, list):
            found.extend(item for item in source if isinstance(item, str))
    seen, unique = set(), []
    for item in found:
        text = clean(item, 500)
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            unique.append(text)
    return unique


def is_combo_record(approved):
    """Combo thuong mai: lieu cua tung thanh phan, khong duoc cong lai."""
    approved = approved or {}
    return approved.get("is_combo") is True or bool(approved.get("combo_components"))


def dose_rows_from_directions(directions, target_liters, extrapolation_rule=None):
    """Doc lieu tu cac dong huong dan da duyet, quy ve binh khach hoi."""
    rows, seen_ratios = [], set()
    for direction in directions:
        source = clean(direction, 500)
        # Một dòng nhãn có nhiều tỷ lệ khác nhau thì không tự chọn tỷ lệ đầu;
        # để label_parser tạo candidate cho người duyệt.
        if _has_multiple_ratios(source):
            continue
        parsed = parse_dose_line(source)
        if not parsed:
            continue
        low, high, unit, litre_low, litre_high = parsed
        if litre_low <= 0 or litre_high <= 0:
            continue
        if litre_low > litre_high:
            litre_low, litre_high = litre_high, litre_low
        inside = litre_low <= target_liters <= litre_high
        if inside:
            # Nhan da ghi san cho khoang binh nay: khong nhan chia gi them,
            # noi dung con so tren nhan. Dung cach doc nhan cua nguoi co nghe.
            low_ml, high_ml = round(low, 2), round(high, 2)
            amount = f"{low:g}–{high:g}{unit}" if low != high else f"{high:g}{unit}"
            calculation = (
                f"{amount} cho bình {litre_low:g}–{litre_high:g}L"
                f" (bình {target_liters:g}L nằm trong khoảng của nhãn)"
                if litre_low != litre_high else f"{amount} cho {litre_high:g}L"
            )
        else:
            if not SETTINGS["quy_doi_ngoai_khoang"] or extrapolation_rule not in {
                    "nearest_endpoint", "preserve_concentration_range"}:
                continue
            if extrapolation_rule == "preserve_concentration_range":
                low_ml = round(low * target_liters / litre_high, 2)
                high_ml = round(high * target_liters / litre_low, 2)
                calculation = (f"Khoảng nồng độ từ nhãn: {low:g}–{high:g}{unit} "
                               f"cho {litre_low:g}–{litre_high:g}L; quy đổi {target_liters:g}L")
            else:
                reference = litre_low if target_liters < litre_low else litre_high
                scale = target_liters / reference
                low_ml, high_ml = round(low * scale, 2), round(high * scale, 2)
                calculation = (f"{low:g}–{high:g}{unit} × {target_liters:g}L / {reference:g}L"
                               if low != high else f"{high:g}{unit} × {target_liters:g}L / {reference:g}L")
        # Bản ghi và hồ sơ duyệt thường nói cùng một tỷ lệ bằng hai cách viết;
        # đưa cả hai vào context chỉ làm model tưởng là hai liều khác nhau.
        signature = (low_ml, high_ml, unit, target_liters, inside)
        if signature in seen_ratios:
            continue
        seen_ratios.add(signature)
        rows.append({
            "target_tank_liters": target_liters,
            # don_vi PHAI doc kem: Rydoxin, Jialeton, A6, Magie Kem... la
            # dang BOT, so o day la GAM chu khong phai ml.
            "don_vi": unit,
            "calculated_ml": high_ml, "calculated_ml_min": low_ml,
            "calculated_ml_max": high_ml, "is_range": low_ml != high_ml,
            "source_tank_liters_min": litre_low, "source_tank_liters_max": litre_high,
            "within_source_range": inside, "is_extrapolated": not inside,
            "extrapolation_rule": extrapolation_rule if not inside else None,
            "calculation": calculation, "approved_source_direction": source,
        })
    return rows


def verified_dose_calculations(comment, approved, dose_intent=True):
    """Scale an approved ml/L ratio to the tank size explicitly asked for.

    Chỉ chạy khi khách thật sự hỏi liều. Không có chốt này thì "đạm cá can 5
    lít giá bao nhiêu" cũng sinh ra một phép quy đổi cho bình 5 lít.
    """
    if not dose_intent or not SETTINGS["tinh_lieu"]:
        return []
    target_liters = target_tank_liters(comment)
    if target_liters <= 0:
        return []
    if is_combo_record(approved):
        # Combo: danh chung mot binh, MOI THANH PHAN GIU NGUYEN LIEU CUA
        # CHINH NO tren cung the tich nuoc (chu du an xac nhan 11/09/2026).
        # Tinh rieng tung thanh phan va gan ten vao phep tinh; tuyet doi
        # khong cong lieu hai thanh phan lai thanh mot con so.
        rows = []
        for component in (approved.get("combo_components") or []):
            name = clean(component.get("ten_san_pham"), 120)
            lines = [clean(item, 500) for item in
                     (component.get("huong_dan_su_dung") or [])]
            for row in dose_rows_from_directions(lines, target_liters,
                    component.get("dose_extrapolation_rule"))[:3]:
                row["combo_component"] = name
                method = clean(component.get("method"), 60)
                if method:
                    row["combo_method"] = method
                row["calculation"] = f"{name}: {row['calculation']}"
                rows.append(row)
        return rows[:6]
    return dose_rows_from_directions(approved_directions(approved), target_liters,
        approved.get("dose_extrapolation_rule"))[:6]
