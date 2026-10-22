"""Sync the common popup assets into both independently installable extensions."""
from pathlib import Path
import argparse

ROOT = Path(__file__).resolve().parent.parent
FILES = ("popup.css", "popup-ux.js", "live-widget.js")

def popup(platform):
    shopee = platform == "extension-shopee"
    values = {
        "PLATFORM": "Shopee" if shopee else "TikTok", "SLUG": "shopee" if shopee else "tiktok",
        "SOURCE": "OBS" if shopee else "TikTok Studio",
        "STATUS": '<span id="connection" class="session-state">Chờ nguồn LIVE</span>' if shopee else '<span class="session-state"><span id="dot"></span><span id="mode">Đang chờ</span></span>',
        "HEALTH_ID": "status" if shopee else "page-status",
        "PIN_BUTTONS": '<button id="pause" class="pin-toggle">Tạm dừng</button><button id="resume" class="pin-toggle" hidden>Tiếp tục</button>' if shopee else '<button id="pause" class="pin-toggle">Tạm dừng</button>',
        "AI_BUTTONS": '<button id="ai-start" class="ai-toggle" aria-pressed="false">Bật AI</button><button id="ai-stop" class="ai-toggle on" aria-pressed="true" hidden>Tắt AI</button>' if shopee else '<button id="ai-toggle" class="ai-toggle" aria-pressed="false">Bật AI</button>',
        "COLLECTOR": "" if shopee else '<div id="collect-status" class="collector-note"></div><div id="api-guard" class="collector-note" role="status" aria-live="polite"></div>',
        "TOOLS": '<details><summary>Chẩn đoán phiên</summary><div id="runtime"></div></details>' if shopee else '''<details><summary>Giỏ sản phẩm &amp; chẩn đoán</summary><label class="note"><input type="checkbox" id="diag-log" checked> Ghi nhật ký chẩn đoán captcha (không ghi cookie, token, nội dung chat)</label><div id="live-cart-warn" class="cart-warn" role="alert" hidden><strong id="live-cart-warn-text"></strong><button id="use-live-cart" type="button">Dùng giỏ của tài khoản LIVE</button></div><div id="live-cart-note" class="note"></div><label class="note">Tên giỏ <input id="cart-name" maxlength="70" placeholder="Giỏ HH:mm dd/mm"></label><label class="note">Lưu vào <select id="cart-target"><option value="new">Giỏ mới</option></select></label><div id="catalog-status"></div><div id="timeline-info"></div><button id="scan-catalog">Quét giỏ sản phẩm</button></details>
<details><summary>Điều khiển thủ công</summary><p class="note">Dùng khi cần căn lại mốc; không đổi video đang phát.</p><button id="start">Bắt đầu lại từ 00:00</button><label for="jump-time">Căn mốc LIVE</label><div class="jump-line"><input id="jump-time" inputmode="numeric" autocomplete="off" placeholder="07:35" aria-describedby="jump-error"><button id="jump">Đến mốc</button></div><div id="jump-error"></div><div class="row"><button id="adjust-back">Lùi 1 giây</button><button id="adjust-forward">Tiến 1 giây</button></div><button id="stop" class="danger">Dừng và bỏ ghim</button></details>''',
        "SCRIPTS": '<script src="core.js"></script><script src="popup.js"></script>' if shopee else '<script src="schedule.js"></script><script src="popup.js"></script>',
    }
    result = (ROOT / "ui/extension/popup.html").read_text(encoding="utf-8")
    for key, value in values.items():
        result = result.replace("{{" + key + "}}",value)
    return result

def sync():
    for platform in ("extension", "extension-shopee"):
        (ROOT / platform / "popup.html").write_text(popup(platform),encoding="utf-8")
    for name in FILES:
        content = (ROOT / "ui" / "extension" / name).read_bytes()
        for platform in ("extension", "extension-shopee"):
            (ROOT / platform / name).write_bytes(content)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    if parser.parse_args().check:
        mismatches = []
        for platform in ('extension','extension-shopee'):
            if (ROOT / platform / 'popup.html').read_text(encoding='utf-8') != popup(platform):
                mismatches.append(platform + '/popup.html')
            for name in FILES:
                if (ROOT / platform / name).read_bytes() != (ROOT / 'ui/extension' / name).read_bytes():
                    mismatches.append(platform + '/' + name)
        if mismatches:
            raise SystemExit('Giao diện chưa đồng bộ: ' + ', '.join(mismatches))
    else:
        sync()
