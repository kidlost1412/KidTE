# Tích hợp Shopee LIVE vào Kid TE

## Quy chuẩn giao diện

- Popup 440 × 600 px; thanh thương hiệu, điều hướng và liên kết trung tâm cố định. Nội dung cuộn trong từng khu vực.
- Hai khu vực: **Điều hành LIVE**, **Công cụ**. AI hiển thị cùng phiên, nguồn và sản phẩm; Điều hành LIVE ưu tiên nguồn phát, vị trí video và sản phẩm đã quan sát trên nền tảng.
- Nguồn thiết kế chung nằm tại `ui/extension/`: CSS popup, tương tác tab và widget shadow DOM. Chạy `backend/.venv/Scripts/python.exe packaging/sync_extension_ui.py` để đồng bộ hai gói độc lập. Đóng gói kiểm tra các bản sao bằng byte, từ chối nếu lệch.
- Chuyển động 120–200 ms chỉ cho chuyển khu vực, mở chi tiết và phản hồi nút. Tôn trọng `prefers-reduced-motion`; không có hiệu ứng nhấp nháy liên tục.
- Tab hỗ trợ mũi tên, Home/End, focus rõ và thuộc tính ARIA. Không xóa trạng thái lỗi thao tác bằng lần cập nhật nền; lỗi có phản hồi riêng.
- “AI đã bật”, “AI sẵn sàng” và “đã gửi” là ba trạng thái khác nhau. Mất cập nhật không giữ đồng hồ hay SKU cũ làm trạng thái hiện tại.
- Widget hai sàn dùng cùng cấu trúc: thương hiệu/sàn, đồng hồ, SKU, nguồn; chi tiết gấp gọn; liên kết về Kid TE.
- Trung tâm LIVE có hai thẻ ngang nhau, nguồn TikTok Studio và OBS độc lập; lưới chuyển một cột trên cửa sổ hẹp. Hỗ trợ theme sáng/tối hiện có.

## Vận hành hiện tại

- Tiện ích Shopee độc lập tự nối phiên, đọc giỏ và ánh xạ kịch bản đã lưu trong Kid TE. TikTok dùng tiện ích riêng, giao diện chung.
- Shopee nhận video từ OBS Media Source; TikTok nhận từ âm thanh tiến trình TikTok Studio. Không dùng nhận diện của sàn này để ghim cho sàn kia.
- Khách hàng, kiến thức, tài liệu, bài học và ưu đãi chủ shop duyệt dùng chung. Ưu đãi nằm trong hồ sơ sản phẩm, không khai hai bản riêng. Catalog, giá quan sát, phiên, quyền điều khiển và lệnh ghim giữ nhãn sàn.
- Sản phẩm, quy cách và luân phiên lấy từ mapping/timeline đã lưu cho đúng video
  và đúng sàn; không dùng tên sản phẩm trong tài liệu làm bảng ghim cố định.
- Ghim lại định kỳ có khoảng bỏ ghim, kiểm tra lại phiên/quyền điều khiển trước khi ghim; trạng thái UI quan sát quyết định thông báo xác nhận.
- AI trả lời giá theo sản phẩm khách hỏi, lịch sử cùng khách trong phiên rồi sản phẩm đang ghim. Chỉ nói giá xác thực của catalog đúng sàn; không mặc định yêu cầu khách nhấn thẻ.
- Câu trả lời làm rõ như “TC01” nối vào câu hỏi còn chờ của đúng khách/phòng LIVE. Giữ bình luận gốc và câu hỏi đã nối ý; câu hỏi mới ưu tiên hơn câu cũ.
- Mất extension context: dừng timer, không gửi thêm lời trả lời hoặc ghi storage từ context đã hủy.

## Thương hiệu và nguồn giao diện

Anh đào gốc; màu chữ mận sẫm, trạng thái thành công xanh sage. Logo đọc K T i E d: T/E chiếm ưu thế, K/i/d nhỏ xen kẽ.

Nguồn chuẩn: `ui/theme/sakura.json`, `ui/extension/`, `assets/kid-te-master.png`.
Đồng bộ bằng `packaging/apply_theme.py`, `packaging/sync_extension_ui.py`, `packaging/sync_brand.py`.
