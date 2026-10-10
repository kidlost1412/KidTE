"""Kiểm cú pháp MỌI module giao diện và khoá các bất biến cấu trúc.

Kiểm ESM rõ ràng bằng --input-type=module qua stdin, không phụ thuộc loại
tệp .js hoặc quyền truy cập thư mục Temp của Windows.
Lỗi này từng che một dấu ngoặc thừa làm trắng trang Sản phẩm.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "web"
MODULES = sorted((WEB / "app").rglob("*.js"))


def test_co_module_de_kiem():
    assert MODULES, "không tìm thấy module giao diện"


@pytest.mark.parametrize("module", MODULES, ids=lambda p: str(p.relative_to(WEB)))
def test_module_dung_cu_phap_esm(module: Path):
    node = shutil.which("node")
    if not node:
        pytest.skip("không có Node")
    result = subprocess.run([node, "--check", "--input-type=module"],
        input=module.read_text(encoding="utf-8"), capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"{module.name}:\n{result.stderr}"


def test_moi_import_tuong_doi_tro_toi_file_co_that():
    for module in MODULES:
        for spec in re.findall(r"""from\s+['"](\.{1,2}/[^'"]+)['"]""", module.read_text(encoding="utf-8")):
            assert (module.parent / spec).resolve().is_file(), f"{module.name} import {spec} không tồn tại"


def test_operator_chi_nap_module_bootstrap():
    page = (WEB / "operator.html").read_text(encoding="utf-8")
    assert '<div id="app">' in page
    assert 'type="module" src="/assets/app/bootstrap.js"' in page
    for css in re.findall(r'href="/assets/(styles/[^"]+)"', page):
        assert (WEB / css).is_file(), css


def test_dashboard_chi_poll_o_mot_noi():
    """Bất biến F03: chỉ một vòng poll /api/dashboard cho cả ứng dụng."""
    hits = [m for m in MODULES if "/api/dashboard" in m.read_text(encoding="utf-8")]
    assert [m.name for m in hits] == ["store.js"]


def test_icon_svg_tao_dung_namespace():
    """Bản cũ tạo <svg> bằng createElement -> toàn bộ icon vô hình."""
    dom = (WEB / "app" / "core" / "dom.js").read_text(encoding="utf-8")
    assert "createElementNS" in dom and "http://www.w3.org/2000/svg" in dom
    assert "document.createElement('svg')" not in "".join(m.read_text(encoding="utf-8") for m in MODULES)


def test_khong_dung_hop_thoai_trinh_duyet():
    """pywebview hiển thị confirm/alert rất xấu và chặn luồng; dùng confirmDialog."""
    for module in MODULES:
        text = module.read_text(encoding="utf-8")
        assert not re.search(r"window\.(confirm|alert|prompt)\(", text), module.name
