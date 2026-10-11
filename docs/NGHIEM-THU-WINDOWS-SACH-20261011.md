# Nghiệm thu cài đặt Windows sạch — 11/10/2026

**Kết quả: bộ cài 1.14.257 P3 r1 đạt 9/9 bước trong phép thử bên dưới; script kết thúc mã 0.** Dùng [bản r1](https://github.com/kidlost1412/KidTE/releases/tag/v1.14.257-p3-r1) để cài mới. Bộ cài 257 ban đầu đã phát hiện lỗi và không đạt nghiệm thu.

## Môi trường và artifact

Windows 10 Enterprise LTSC Evaluation x64, build 19044, cài mới từ ISO Microsoft trong máy ảo QEMU/WHPX; tài khoản `KidTEQA` không thuộc Administrators, không chạy nâng quyền. Khôi phục snapshot toàn bộ ổ Windows trước từng lần thử; không chia sẻ thư mục runtime/dữ liệu của máy vận hành. Trước lần thử r1, không có Python/FFmpeg trên PATH và không có WebView2 trong các registry view được kiểm tra. Không thay đổi kích hoạt Windows Evaluation.

Bộ cài được tải **trực tiếp, không đăng nhập** từ GitHub trong máy thử, không chép file build trên host vào để cài:

- File: `KidTE-Setup-1.14.257-cpu-p3-r1.exe`.
- Dung lượng: **48.995.455 byte**, khoảng 46,73 MiB / 49 MB. Chưa tính WebView2 nếu máy thiếu; Microsoft tải runtime riêng.
- SHA256: `6c4a426c41822e8e4e5d6e6d83c4b1234b4b3fa2d87cbd8b910e0adad23e0992`.
- Installer/manifest từ commit `3fdff65de00e05fe69f822080f8e6bd5db841878`. Core 1.14.257 dùng lại ZIP từ commit `6a3447dd21dad0b7a01705ffeaaba1413ed6616d`; các commit sau chỉ sửa installer. URL/hash core và catalog beta ký Ed25519 sequence 14 giữ nguyên. Không ghi đè asset cũ.

## Các bước đã đạt

| Bước | Kết quả |
|---|---|
| Cài mới vào đường dẫn có dấu/khoảng trắng; mở managed core 1.14.257 | Đạt; ban đầu chỉ có core |
| Tạo cửa sổ ứng dụng native | Đạt |
| Nội dung WebView2 sẵn sàng | Đạt; `ready=complete`, `main=true`, `loading=false`; có ảnh chụp giao diện |
| Chọn lại đúng phiên bản core đang dùng | Đạt; không yêu cầu restart |
| Tải/cài bốn gói bổ sung từ GitHub, giải phụ thuộc và áp dụng qua restart | Đạt; tổng 402.272.698 byte; core và bốn gói đã cài |
| Chẩn đoán worker đóng gói | Đạt; worker probe `ok=true`, chẩn đoán `module-faster_whisper=ok` |
| Cài lại không truyền thư mục mới | Đạt; dùng lại đường dẫn trước, giữ đủ gói và hash dữ liệu mẫu |
| Gỡ cài đặt im lặng | Đạt; thư mục App được gỡ, hash dữ liệu mẫu giữ nguyên |
| Cài và mở lại sau khi gỡ | Đạt; hash dữ liệu mẫu giữ nguyên |

WebView2 155.0.4283.45 được bootstrapper Microsoft cài theo người dùng. Log lần đầu ghi `OK|runtime-present`; hai lần cài sau ghi `OK|already-installed`. Worker probe nạp các thư viện đóng gói; CAPTCHA probe xác nhận 10 tài nguyên và tìm FFmpeg ở gói media-tools sau khi bỏ biến môi trường app cha. Không tải model cho các probe này.

## Lỗi tìm được và sửa

Ở bản 256: thiếu WebView2 làm cửa sổ đứng ở màn hình đang mở; cài lại cùng phiên bản vẫn báo cần restart; chẩn đoán báo thiếu faster-whisper dù worker đóng gói chạy được. Các phép thử tương ứng đều đạt với core 257/bộ cài r1.

Lần cài thực bản 257 ban đầu phát hiện lỗi mới: helper đổi `|` thành `/` trong file kết quả, khiến installer từ chối kết quả thành công. R1 giữ đúng protocol. Test hồi quy ghi/đọc file ASCII thật đạt trên PowerShell 5.1; đưa lại dòng lỗi trong bản sao QA làm test thất bại đúng chỗ, không gọi mạng hoặc cài runtime trên host.

Script QA cũng được sửa lỗi ghép argument `/LOG=`, lấy ExitCode và giả định sai về `KidTEVideo --help`. Đây là lỗi của phép thử, không được tính thành lỗi sản phẩm.

## Giới hạn nghiệm thu

Đây là nghiệm thu cài đặt/cập nhật gói trên Windows 10 x64, không phải chứng nhận mọi chức năng hoặc mọi phiên bản Windows. Chưa nghiệm thu wizard tương tác, Windows 11, phiên LIVE thực, solver/provider, tải model, GPU hoặc thiết bị âm thanh trong máy này. Gói đồng bộ video đã được tải/cài và kiểm sự hiện diện; chưa xác nhận hoạt động LIVE. Trong baseline 256, lệnh `--help` sai đã khởi chạy ngắn dịch vụ đồng bộ trên máy chưa cấu hình trước khi dừng; không ghi network trace, nên không khẳng định không có lưu lượng mạng từ dịch vụ đó.

Kiểm thử source trước đó: 1.735 test đạt, 6 skip, 49 subtest; 82 web test và 18 suite tiện ích. Prerequisite r1 có 8 nhóm kiểm tra mô phỏng/serialized-file; 16 kiểm tra packaging/diagnostics liên quan đã đạt trước bản vá protocol. Các kết quả này không thay thế nghiệm thu luồng LIVE và GPU ở đợt riêng.
