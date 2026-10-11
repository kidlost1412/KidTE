# Kid TE · Shopee LIVE 0.12.2

Mặc định cấu hình trong README này có thể khác trạng thái dữ liệu đang vận hành; chi tiết thực tế nằm trong tài liệu kiến thức nội bộ của người vận hành (không nằm trong kho công khai vì mô tả cấu hình thật).

Mở trang quản lý Shopee, tiện ích tự kết nối Kid TE và tự đọc giỏ. Bộ nghe của Kid TE nhận ra video nào thì dùng kịch bản đã lưu của video đó. Không cần mở popup để quét giỏ, chọn video hoặc chọn từng sản phẩm.

## Vận hành

1. Chạy Kid TE. Trong **Hệ thống → Nguồn LIVE**, cấu hình Shopee theo nguồn phương tiện OBS và TikTok theo âm thanh tiến trình TikTok Studio. Hai sàn có vị trí/video độc lập, có thể phát hai video khác nhau. Nếu chủ động dừng nguồn thì không tự bật lại trong lượt mở app ấy.
2. Mở trang Shopee `/pc/preview?session=...` hoặc `/pc/live?session=...`. Widget Kid TE hiện trạng thái ngay trên trang. Preview tự chuẩn bị giỏ và ánh xạ; khi chuyển sang LIVE sẽ ghim theo vị trí video.
3. Ánh xạ tự lưu trong `data/machine/shopee_live_mappings.json` của Kid TE. Khi đổi video/phiên/giỏ, tiện ích tự cập nhật.
4. Popup có hai khu vực **Điều hành LIVE** và **Công cụ**. Điều hành LIVE ưu tiên nguồn phát, đồng hồ, sản phẩm đã xác nhận đang ghim cùng công tắc AI và kết quả xử lý; Công cụ dành cho chẩn đoán. Tạm dừng được giữ qua lần mở Chrome sau; bấm Tiếp tục để bật lại. Liên kết mở đúng trang nguồn hoặc hộp thư Shopee trong Kid TE.

Quy tắc của chủ shop: Top Bo One/Bọ Trĩ và Sạch Sâu ghim TC01; Combo Đạm Cá Humic ghim Đạm Cá 5L và nhánh luân phiên Đạm Cá 1L. ID lấy từ giỏ thực tế. Ánh xạ đã lưu còn khớp được dùng lại; giỏ thiếu hoặc có nhiều sản phẩm khớp mà chưa có ID đã xác định thì báo rõ, không đoán.

Tự ghim chỉ chạy trên một tab điều khiển. Tab còn lại theo dõi. Mất Kid TE, tín hiệu nhận diện yếu/cũ, phiên chưa LIVE hoặc sản phẩm biến mất đều ngừng lệnh mới và tự thử lại. Nhịp làm mới mặc định 20 giây: bỏ ghim, chờ 800ms rồi ghim sản phẩm đúng với vị trí mới nhất. Sau lệnh, đọc trạng thái `showingProduct` của UI Shopee để xác nhận đúng cả shop và sản phẩm; nếu không đọc được thì báo chờ xác nhận. Lưu 100 thao tác gần nhất cùng kết quả xác nhận trong `shopeePinLog`.

## AI và dữ liệu chung

DeepSeek có công tắc bật trả lời thật và tắt. Không còn chế độ xem trước hoặc bắt tạo câu thử. Ý định bật AI được giữ qua lần kết nối lại; chỉ tab điều khiển của phiên LIVE được gửi. Chỉ xử lý bình luận mới của khách thật, ghi đã gửi khi có `message_id` và receipt từ Kid TE. Poll hợp lệ không có trường `message` nghĩa là chưa có câu mới. Nếu `data/machine/queue_mode.json` bật (`{"enabled": true}`, mặc định tắt), tiện ích đưa bình luận vào hàng chờ bền của Kid TE và lấy việc qua `queue/next`; mỗi lượt gồm 1–3 tin, tối đa 150 đơn vị UTF-16 mỗi tin. Popup phân biệt AI đã bật, đang xử lý và có receipt đã gửi; giữ lần xử lý gần nhất để trạng thái chờ không che lỗi/bỏ qua/receipt.

Khi hỏi giá, Kid TE ưu tiên sản phẩm/dung tích khách chỉ rõ, rồi lịch sử cùng khách trong phiên gần nhất, sau đó mới dùng sản phẩm đang ghim. Chỉ nói giá từ giỏ Shopee mới cập nhật của đúng phiên, có tiền tệ VND và đúng sản phẩm bán. Dùng giá trước giảm nếu có, nếu không dùng giá niêm yết theo quy tắc shop. Không lấy giá TikTok, giá hàng tặng hoặc giá khách tự nêu làm dữ kiện. Chưa rõ dung tích thì hỏi lại; thiếu dữ kiện giá xác thực thì không tự dựng số hoặc gửi mẫu yêu cầu khách nhấn thẻ xem giá. Ưu đãi/giao hàng/tồn kho cũng cần dữ liệu có căn cứ. Tư vấn kỹ thuật dùng DeepSeek và kiến thức đã duyệt.

Shopee/TikTok dùng chung cơ sở dữ liệu khách, kiến thức, tài liệu, quy tắc và bài học trong Kid TE. Giữ định danh nguồn sàn để không ghép nhầm người. Giá/ưu đãi và receipt theo sàn.

## Cập nhật

Trong `chrome://extensions`, tải lại **Kid TE - Shopee LIVE** từ thư mục này. Tiện ích tự nối vào tab quản lý đang mở; không cần tải lại trang LIVE. Worker dừng khi Chrome vô hiệu hóa context và chặn gửi AI sau khi dừng. Bản cập nhật giữ lựa chọn AI của người vận hành; cài mới mặc định tắt AI. Mất cập nhật thì đồng hồ/SKU hiển thị chưa xác định, không giữ dữ liệu cũ làm hiện tại. Mục Errors có thể giữ lỗi lịch sử của bản cũ. Tiện ích TikTok vẫn cài riêng nhưng dùng cùng quy chuẩn giao diện.
