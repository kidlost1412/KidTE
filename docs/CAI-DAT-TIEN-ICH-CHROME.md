# Cài tiện ích Chrome cho Kid TE

Ở bản cài đặt, tiện ích nằm trong thư mục `extension` bên cạnh `KidTE.exe`;
ở bản mã nguồn chạy bằng `KidTE.exe`, tiện ích nằm trong thư mục `extension` ở gốc
mã nguồn. Trong app, mở **Cài đặt & hệ thống → Cài đặt**, thẻ **Tiện ích Chrome · TikTok**, rồi bấm
**Mở hướng dẫn cài tiện ích**. Chrome mở `chrome://extensions/` và Windows
Explorer chỉ đúng thư mục cần chọn.

1. Bật **Chế độ dành cho nhà phát triển**.
2. Bấm **Tải tiện ích đã giải nén**.
3. Chọn đúng thư mục `extension` vừa được mở.
4. Ghim Kid TE lên thanh công cụ và kiểm trạng thái kết nối trong app.

## Shopee LIVE

Tiện ích Shopee độc lập nằm trong thư mục `extension-shopee` cạnh `extension`.
Cài riêng bằng thẻ **Tiện ích Chrome · Shopee** (nút **Mở hướng dẫn cài tiện ích Shopee**) hoặc tự chọn thư mục `extension-shopee` tại `chrome://extensions/`. Bản 0.12.2 tự kết nối khi mở trang quản lý, tự đọc giỏ, tự dùng video/kịch bản do Kid TE nhận diện và lưu ánh xạ vào Kid TE. Không cần bấm quét hoặc chọn sản phẩm mỗi phiên. Widget trên trang báo trạng thái. Popup có hai khu vực **Điều hành LIVE** và **Công cụ**, cùng quy chuẩn với tiện ích TikTok (3.10.1). Trạng thái AI và công tắc trả lời nằm ngay trong Điều hành LIVE.

Top Bo One/Bọ Trĩ/Sạch Sâu dùng TC01; Combo Đạm Cá Humic dùng Đạm Cá 5L và nhánh luân phiên 1L theo quy tắc chủ shop. Sản phẩm thiếu hoặc trùng tên chưa xác định ID sẽ được báo, chưa gửi lệnh cho mục đó.

DeepSeek có công tắc bật/tắt trả lời thật; không có bước bắt buộc xem trước. Cài mới mặc định tắt AI; cập nhật giữ lựa chọn hiện tại. Hai sàn dùng chung kho khách, kiến thức, tài liệu và bài học trong Kid TE, với nhãn nguồn riêng. Giá chỉ lấy từ catalog đúng sàn/phiên: sản phẩm khách hỏi → lịch sử cùng khách → sản phẩm đang ghim; không dùng câu yêu cầu nhấn thẻ thay cho trả lời giá.

Trong **Cài đặt & hệ thống → Nguồn LIVE theo sàn**, chọn nguồn phương tiện OBS cho Shopee và âm thanh tiến trình TikTok Studio cho TikTok. Hai nguồn nhận diện video độc lập. Sau lệnh ghim, tiện ích đọc lại `showingProduct` trên Shopee để xác nhận; thiếu trạng thái đọc được thì báo chờ xác nhận, không báo đã ghim.

Cập nhật Kid TE bản mới thì bấm **Tải lại** trên từng thẻ tiện ích ở
`chrome://extensions/`. Tiện ích tự nối lại tab quản lý đang mở; không cần tải lại
trang LIVE. Kiểm tra kết nối, nguồn video, sản phẩm đã xác nhận và công tắc AI
trong popup sau khi cập nhật.

## Máy do công ty quản trị

Quản trị viên có thể phân phối cùng thư mục extension bằng chính sách Chrome
Enterprise nội bộ. Mã extension và nguồn cập nhật phải do tổ chức sở hữu; không
đưa API key hay dữ liệu người dùng vào gói extension. Nếu chưa có hạ tầng chính
sách, dùng quy trình thủ công ở trên.
