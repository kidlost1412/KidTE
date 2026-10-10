"""So sánh và gộp gói kiến thức chung với hồ sơ riêng của máy (Gói 13).

``apply_package()`` bản cũ thay NGUYÊN tệp bằng nội dung gói. Điều đó vi phạm
hai chốt của kế hoạch:

  - "Nhập gói cho xem: thêm mới, thay đổi, giữ nguyên và xung đột."
  - "Dữ liệu riêng không bị ghi đè bởi trường trống trong gói."

Và nó có một hậu quả rất cụ thể: ``legacy_profile_id`` — đường nối tới hồ sơ
kỹ thuật đã duyệt — nằm ngay trong tệp này. Một gói thiếu trường đó sẽ xoá
sạch liên kết, tái tạo lại đúng sự cố F13 (AI trả lời liều dùng mà không có
hồ sơ kỹ thuật đã duyệt).

Quy tắc gộp ở đây:
  * Trường gói KHÔNG có  -> giữ nguyên bản tại máy.
  * Trường gói có nhưng RỖNG -> giữ nguyên bản tại máy (không xoá bằng trống).
  * Hai bên khác nhau và bản tại máy đã được sửa -> XUNG ĐỘT, hỏi người dùng.
  * Còn lại -> lấy theo gói.
"""
from __future__ import annotations

import copy


# Trường do từng máy tự quản, gói chung không bao giờ được đụng tới.
TRUONG_RIENG_CUA_MAY = frozenset({
    "legacy_profile_id",     # đường nối tới hồ sơ kỹ thuật đã duyệt (xem F13)
    "catalog_status",
    "approved_for_live",
    "approved_by",
    "approved_at",
    "gia_goc_duyet",
    "live_promotion",
    "quy_cach_dong_goi",
    "canh_bao_quy_cach",
    "listing_aliases",
})


def _rong(gia_tri) -> bool:
    return gia_tri in (None, "", [], {})


def _san_pham_theo_ma(noi_dung: dict) -> dict:
    ket = {}
    for nhom in (noi_dung or {}).get("nhom_san_pham", []) or []:
        for san_pham in nhom.get("san_pham", []) or []:
            ma = str(san_pham.get("san_pham_id") or "").strip()
            if ma:
                ket[ma] = san_pham
    return ket


def so_sanh(noi_dung_hien_tai: dict, noi_dung_goi: dict) -> dict:
    """Xem gói sẽ đổi những gì, trước khi áp dụng bất cứ thứ gì."""
    tai_may = _san_pham_theo_ma(noi_dung_hien_tai)
    trong_goi = _san_pham_theo_ma(noi_dung_goi)

    them_moi, thay_doi, giu_nguyen, xung_dot, chi_co_tai_may = [], [], [], [], []

    for ma, ban_goi in sorted(trong_goi.items()):
        ban_may = tai_may.get(ma)
        if ban_may is None:
            them_moi.append({"san_pham_id": ma,
                             "ten_san_pham": ban_goi.get("ten_san_pham", "")})
            continue
        cac_truong = []
        for khoa, gia_tri_goi in ban_goi.items():
            if khoa in TRUONG_RIENG_CUA_MAY:
                continue
            gia_tri_may = ban_may.get(khoa)
            if _rong(gia_tri_goi):
                continue                      # không xoá bằng trống
            if gia_tri_may == gia_tri_goi:
                continue
            cac_truong.append({
                "truong": khoa,
                "tai_may": gia_tri_may,
                "trong_goi": gia_tri_goi,
                "co_ban_rieng": not _rong(gia_tri_may),
            })
        if not cac_truong:
            giu_nguyen.append({"san_pham_id": ma,
                               "ten_san_pham": ban_may.get("ten_san_pham", "")})
        elif any(t["co_ban_rieng"] for t in cac_truong):
            # Bản tại máy đã có nội dung khác — chỉ chỗ này mới cần người quyết.
            xung_dot.append({"san_pham_id": ma,
                             "ten_san_pham": ban_may.get("ten_san_pham", ""),
                             "cac_truong": cac_truong})
        else:
            thay_doi.append({"san_pham_id": ma,
                             "ten_san_pham": ban_may.get("ten_san_pham", ""),
                             "cac_truong": cac_truong})

    for ma, ban_may in sorted(tai_may.items()):
        if ma not in trong_goi:
            chi_co_tai_may.append({"san_pham_id": ma,
                                   "ten_san_pham": ban_may.get("ten_san_pham", "")})

    return {
        "them_moi": them_moi,
        "thay_doi": thay_doi,
        "giu_nguyen": giu_nguyen,
        "xung_dot": xung_dot,
        "chi_co_tai_may": chi_co_tai_may,
        "can_nguoi_quyet": len(xung_dot),
        "tom_tat": {
            "them_moi": len(them_moi), "thay_doi": len(thay_doi),
            "giu_nguyen": len(giu_nguyen), "xung_dot": len(xung_dot),
            "chi_co_tai_may": len(chi_co_tai_may),
        },
    }


def gop(noi_dung_hien_tai: dict, noi_dung_goi: dict,
        quyet_dinh_xung_dot: dict | None = None) -> dict:
    """Dựng nội dung sau khi nhập, KHÔNG ghi đĩa.

    ``quyet_dinh_xung_dot``: {san_pham_id: "giu_ban_may" | "lay_theo_goi"}.
    Mặc định cho mục xung đột là GIỮ BẢN TẠI MÁY — không tự ghi đè công sức
    soạn tại chỗ.
    """
    quyet_dinh = quyet_dinh_xung_dot or {}
    khac_biet = so_sanh(noi_dung_hien_tai, noi_dung_goi)
    ma_xung_dot = {m["san_pham_id"] for m in khac_biet["xung_dot"]}
    trong_goi = _san_pham_theo_ma(noi_dung_goi)

    ket_qua = copy.deepcopy(noi_dung_hien_tai)
    tai_may = _san_pham_theo_ma(ket_qua)
    da_doi = []

    for ma, ban_goi in trong_goi.items():
        ban_may = tai_may.get(ma)
        if ban_may is None:
            continue                       # thêm mới xử lý riêng bên dưới
        if ma in ma_xung_dot and quyet_dinh.get(ma) != "lay_theo_goi":
            continue                       # giữ bản tại máy
        for khoa, gia_tri_goi in ban_goi.items():
            if khoa in TRUONG_RIENG_CUA_MAY or _rong(gia_tri_goi):
                continue
            if ban_may.get(khoa) != gia_tri_goi:
                ban_may[khoa] = copy.deepcopy(gia_tri_goi)
                da_doi.append(f"{ma}.{khoa}")

    # Sản phẩm chỉ có trong gói: thêm vào nhóm đầu tiên.
    moi = [m["san_pham_id"] for m in khac_biet["them_moi"]]
    if moi:
        # Danh sách rỗng là falsy: `... or []` sẽ tạo danh sách MỚI không gắn vào
        # kết quả, và máy trắng nhập gói xong vẫn trắng.
        if not isinstance(ket_qua.get("nhom_san_pham"), list):
            ket_qua["nhom_san_pham"] = []
        nhom = ket_qua["nhom_san_pham"]
        if not nhom:
            nhom.append({"nhom": "Từ gói kiến thức chung", "san_pham": []})
        dich = nhom[0].setdefault("san_pham", [])
        for ma in moi:
            dich.append(copy.deepcopy(trong_goi[ma]))
            da_doi.append(f"{ma}.(thêm mới)")

    return {"noi_dung": ket_qua, "khac_biet": khac_biet, "da_doi": sorted(da_doi)}
