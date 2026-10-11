# Tiện ích Chrome Kid TE

Cài đặt: xem `docs/CAI-DAT-TIEN-ICH-CHROME.md`. Tiện ích là thư mục `extension`
cạnh ứng dụng Kid TE. Bản 3.10.1 tự nối lại tab quản lý khi cập nhật tiện ích;
không cần tải lại trang đang LIVE.

Tiện ích làm việc trên trang **TikTok Shop LIVE Manager** và gọi thẳng API mà
chính trang TikTok đã khởi tạo (không bấm nút trên giao diện, không đọc DOM),
nên cookie và xác thực vẫn do TikTok xử lý. Nó nói chuyện với ứng dụng Kid TE
ở `127.0.0.1:8793`; không có khoá API nào nằm trong tiện ích.

## Ba việc tiện ích làm

**1. Thu bình luận (luôn bật).** Ngay khi mở tab LIVE Manager, tiện ích nghe sự
kiện bình luận của TikTok và gửi toàn bộ về Kid TE (Hộp thư), kể cả khi AI đang
tắt, kể cả câu chủ shop tự gõ. Backend chưa chạy thì dữ liệu được giữ trong trình
duyệt và gửi lại khi nối lại được. Mở nhiều tab thì chỉ một tab giữ vai thu.

**2. Tự ghim sản phẩm theo lịch của video.** Lịch ghim là của từng video (tạo và
duyệt trong Kid TE → Video & lịch ghim). Trong **Hệ thống → Nguồn LIVE**, cấu
hình nguồn TikTok Studio: ứng dụng nghe âm thanh tiến trình để nhận diện vị trí;
tiện ích tự nạp đúng lịch của video đó và căn đồng hồ khi lệch quá 3 giây.
Cứ mỗi chu kỳ (mặc định 20 giây) tiện ích bỏ ghim rồi ghim lại để sản phẩm luôn
nổi; mỗi lệnh chỉ được coi là xong khi API TikTok xác nhận.

- Chủ shop tự ghim tay một sản phẩm → sản phẩm đó được ưu tiên 60 giây.
- Tab LIVE Manager thấy phòng đang LIVE thì tự nhận vai điều khiển và tự bắt đầu
  (như Shopee), không cần bấm. Bấm **Dừng và bỏ ghim** thì không tự bắt đầu lại cho tới khi phòng đổi.
- Chưa có đồng bộ âm thanh: trong **Công cụ → Điều khiển thủ công**, bấm **Bắt đầu lại từ 00:00**
  khi video bắt đầu, hoặc nhập mốc đang phát rồi **Đến mốc**; **Lùi/Tiến 1 giây** để căn tinh.
- **Dừng và bỏ ghim** kết thúc bộ hẹn giờ và bỏ ghim.

**3. AI trả lời bình luận (bấm Bật AI).** Trước khi bật, tiện ích quét toàn bộ giỏ
hàng (làm mới mỗi 60 giây). Mỗi bình luận được gửi sang Kid TE kèm sản phẩm đang
ghim và vị trí video; câu trả lời gửi lên TikTok có `@tên khách`, tối đa 150 ký
tự, cách nhau tối thiểu 2,5 giây. TikTok đòi xác minh (captcha) hoặc chặn gửi thì
tiện ích tạm dừng gửi và báo trên widget/popup. Một lượt có thể gồm 1–3 tin (giao thức v2). Nếu `data/machine/queue_mode.json` bật (`{"enabled": true}`),
tiện ích đưa bình luận vào hàng chờ của Kid TE ngay khi nhận và lấy việc qua `queue/next`; bình luận chưa gửi được tin đầu sau 120 giây
thì bỏ. Các điều kiện dữ kiện và chính sách giá được kiểm trước và sau sinh câu; một số vi phạm sau model chỉ được ghi advisory, nên chưa bảo đảm chặn tuyệt đối mọi câu sai. Chi tiết mã và giới hạn kiểm chứng nằm trong tài liệu kiến thức nội bộ của người vận hành (không nằm trong kho công khai vì mô tả cấu hình thật).

## Popup và widget 3.10.1

- **Điều hành LIVE**: nguồn phát, đồng hồ, sản phẩm đang ghim, mốc kế và công tắc AI cùng kết quả xử lý. Tín hiệu cũ không hiển thị như hiện tại; bật AI khác với đã gửi. Cập nhật giữ lựa chọn AI hiện tại; cài mới mặc định tắt.
- **Công cụ**: làm mới giỏ sản phẩm và căn mốc thủ công khi cần; không bắt thao tác này trong quy trình tự động.
- Widget cùng cấu trúc với Shopee, có chi tiết gấp gọn và liên kết Kid TE. Nguồn Shopee OBS và TikTok Studio độc lập, có thể chạy hai video khác nhau.
- Điều hướng bàn phím, trạng thái đang xử lý và phản hồi lỗi riêng; hỗ trợ giảm chuyển động. Liên kết nguồn/hộp thư mở đúng trang và đúng sàn trong Kid TE.

## API TikTok dùng tới

- Phòng LIVE: `GET /api/v1/streamer_desktop/live_room_info/get`
- Danh sách + trạng thái ghim: `GET /api/v1/streamer_desktop/live_product/list`
- Ghim/bỏ ghim: `POST /api/v1/streamer_desktop/live_product/pin` (`op` 1 = ghim, 2 = bỏ)

Tiện ích ưu tiên module client hiện tại của TikTok và tự tìm lại module theo
endpoint nếu TikTok đổi mã module sau một đợt phát hành.

## Kiểm thử (người phát triển)

    node test-comments.cjs      (và các test-*.cjs khác; test-content và test-popup cần Playwright)
    .venv\Scripts\python.exe backend\tools\run_tests.py --suite extension
