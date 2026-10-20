# Kid TE 1.15.22

Bản này thay cho 1.15.21 (1.15.21 không phát hành), nên gồm toàn bộ thay đổi của 1.15.21.

**Khi cập nhật bản này**
- Cập nhật trong app như thường (không cần chạy lại bộ cài).
- Tải lại tiện ích TikTok tại `chrome://extensions` (TikTok 3.11.10, gói tiện ích 1.0.12). Tiện ích Shopee không đổi.

**Sửa lỗi quét giỏ trước LIVE / tự ghim báo 0 sản phẩm**
- Không còn nhầm room_id 0 (chưa LIVE) là phòng thật; đọc đúng room_id số lớn.
- Quét giỏ trước LIVE thử thêm cách đọc giỏ của TikTok, nên lấy được sản phẩm đã thêm vào giỏ trước khi phát.
- Giỏ trống thì báo một lỗi rõ ràng thay cho "0 sản phẩm".
- Tiện ích luôn trả lời app, không còn treo im lặng.

**Bật/tắt nhanh TikTok từ app**
- Nút bật/tắt AI và tự ghim trong app nay áp dụng ngay vào tab LIVE đang chạy và báo đúng kết quả.

**Nghe video**
- Nghe video 1 lần đầu phiên, 30 phút sau mới nghe lại (trước đây 5 phút).
- Nút "Nghe lại" đã sửa.

**Giỏ hàng riêng theo tài khoản LIVE**
- Mỗi tài khoản LIVE có giỏ sản phẩm riêng.
- Chọn giỏ ở trang Sản phẩm và Studio Edit.

**Nhật ký chẩn đoán captcha**
- Tiện ích TikTok ghi nhật ký chẩn đoán captcha trình duyệt (không ghi cookie, token, nội dung chat); tắt được trong popup tiện ích.
