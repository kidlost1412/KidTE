# Gia hạn catalog cập nhật (không cần build bản mới)

Catalog `kidte-catalog-v2.json` (tag `updates-beta-p4`) có hạn khoảng 90 ngày kể từ lần ký. Hết hạn thì mọi máy từ chối cập nhật
(`catalog_expired_or_not_yet_valid`). Khi còn dưới 21 ngày, trang Cập nhật trong app hiện một dòng nhắc (`/api/updates` trả
`catalog_expiring_soon`, `catalog_days_left`, `catalog_expires_at`). Nên gia hạn khoảng 60 ngày sau lần ký gần nhất.

Gia hạn = catalog y hệt (cùng gói, URL, hash), `sequence` +1, `issued_at`/`expires_at` mới. Không tạo tag phiên bản, không push `main`.

## Các bước (PowerShell, chạy ở `D:\KidTE-NguonMa`)

```powershell
$dir = "D:\KidTE-NguonMa\PhatHanh\renew-catalog"
New-Item -ItemType Directory -Force $dir | Out-Null

# 1. Tải catalog đang công bố (ẩn danh)
Invoke-WebRequest "https://github.com/kidlost1412/KidTE/releases/download/updates-beta-p4/kidte-catalog-v2.json" -OutFile "$dir\published.json" -UseBasicParsing

# 2. Tạo payload gia hạn (chưa ký): sequence +1, hạn mới 90 ngày
.\.venv\Scripts\python.exe packaging\update_release.py renew-catalog --published "$dir\published.json" --out $dir

# 3. Ký bằng đường ký hiện có (khoá nằm ngoài repo)
.\.venv\Scripts\python.exe packaging\update_release.py sign "$dir\catalog-payload.json" --private-key D:\KidTE-KhoaKy\<tệp-khoá> --out "$dir\kidte-catalog-v2.json"

# 4. Kiểm lại: chữ ký, verify_catalog(minimum_sequence = cũ+1), nội dung gói không đổi, hạn mới
.\.venv\Scripts\python.exe packaging\update_release.py verify-renewed "$dir\kidte-catalog-v2.json" --published "$dir\published.json"

# 5. Xem kế hoạch (không ghi gì), rồi đăng thật
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\publish_catalog.ps1 -Folder $dir -WhatIf
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\publish_catalog.ps1 -Folder $dir
```

`publish_catalog.ps1` tự chạy lại `verify-renewed` và `check-publish` trước khi upload, chỉ thay asset `kidte-catalog-v2.json`
trên tag `updates-beta-p4` (upload tạm, xoá cũ, đổi tên), rồi tải lại ẩn danh để đối chiếu sha256. Bất kỳ bước kiểm nào lỗi thì dừng, không thay catalog.

Sau khi đăng, vào app: Cài đặt, Cập nhật, Kiểm tra lại; dòng nhắc hết hạn biến mất.

## Dọn release cũ (`prune_releases.ps1`)

Script tải catalog của MỌI feed `updates-*` đang có trên GitHub; tải lỗi (hoặc thiếu feed bắt buộc `updates-beta-p4`) thì dừng, không xoá gì.
Tag nào còn được catalog tham chiếu (gồm tag chứa runtime) không bao giờ bị xoá.
