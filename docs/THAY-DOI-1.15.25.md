# Kid TE 1.15.25

## Cập nhật

- Cập nhật app và gói tiện ích trong Cài đặt → Cập nhật, rồi khởi động lại app.
- Tải lại tiện ích TikTok tại `chrome://extensions` (TikTok 3.11.13, gói tiện ích 1.0.15). Tiện ích Shopee không đổi.

## Giỏ hàng

- Sửa lỗi quét giỏ ở tab khác (chưa LIVE, TikTok không báo tài khoản) ghi đè mất giỏ cũ: giờ app khớp giỏ theo sản phẩm (trùng từ 80% trở lên thì cập nhật giỏ đó), khác hẳn thì tạo giỏ mới, giỏ cũ giữ nguyên.
- Trang Sản phẩm có nút "Khôi phục bản trước": trả giỏ về bản trước lần quét ghi đè gần nhất (mỗi giỏ giữ tối đa 10 bản).
- Popup tiện ích báo rõ đã lưu vào giỏ nào; nếu là giỏ mới thì ghi rõ, có thể đổi tên/xoá ở trang Sản phẩm.

## Bộ giải CAPTCHA

- Trạng thái hiện "Sẵn sàng" khi chỉ có lỗi lẻ. Lỗi 400 nghĩa là ảnh CAPTCHA không hợp lệ, không phải lỗi key; lỗi gần nhất vẫn ghi nhỏ bên dưới để tham khảo.

## Ảnh sản phẩm

- Link ảnh lỗi (hết hạn, 403/404) không bị tải lại dồn dập: thử lại sau 30 giây, 2 phút, 10 phút, rồi tối đa 30 phút một lần. Có link ảnh mới (quét lại giỏ) thì thử ngay.
- Giới hạn số ảnh tải cùng lúc; mở trang không làm treo app khi mạng chậm (mỗi ảnh chờ tối đa khoảng 8 giây, ảnh tải xong sẽ hiện ở lần mở sau).
