# Cài Kid TE trên Windows

Cần Windows 10/11 64-bit. Không cần quyền quản trị.

Áp dụng cho Kid TE 1.15.x (dòng gói P4: chương trình tách thành runtime + app + tiện ích Chrome, cập nhật từng phần trong app).

**WebView2**: Kid TE mở cửa sổ bằng Microsoft Edge WebView2. Bộ cài tự kiểm tra; máy thiếu thì tải bộ cài nhỏ của Microsoft, xác minh chữ ký rồi cài bằng quyền người dùng hiện tại (cần mạng). Nếu không cài được (máy không có mạng, mạng chặn), bộ cài giải thích bằng tiếng Việt và cho chọn **cài tiếp**: khi chưa có WebView2, Kid TE mở giao diện bằng trình duyệt mặc định và nhắc cách cài. Máy không có mạng: chép **Evergreen Standalone Installer x64** từ [trang WebView2 của Microsoft](https://developer.microsoft.com/microsoft-edge/webview2/) sang và cài trước.

**Lần mở đầu tiên** (cần mạng): Kid TE tự tải các gói tính năng — công cụ video, giải CAPTCHA, đồng bộ video, phân tích/chép lời video; máy có card NVIDIA phù hợp thì tải thêm gói tăng tốc. Tiến độ xem ở **Cài đặt → Cập nhật**. Xong thì đóng và mở lại app khi được nhắc. Model chép lời tải riêng ở trang **Chép lời** (chọn model đánh dấu "Hợp với máy này").

Trình giải CAPTCHA nằm trong `App\packages\captcha-solver\<phiên bản>\Captcha`.
Mở Kid TE → **Giải CAPTCHA** để nhập SadCaptcha key. Windows sẽ hỏi quyền
Administrator khi khởi chạy solver; Kid TE không lấy quyền đó cho các chức năng
khác. Chỉ một solver được chạy trong mỗi phiên Windows. Nếu key hết lượt,
solver dừng và chờ key mới. Kết quả kéo phải được xác nhận trong TikTok LIVE
Studio; thông báo hộp thoại biến mất chưa đủ để kết luận giải thành công.
Nếu cần nhận cảnh báo và video, nhập bot token cùng Chat ID dạng số trên cùng
trang rồi khởi động lại solver. Key và token cần nhập lại trên máy Windows mới.

## Cài đặt

1. Tải bộ cài `KidTE-Setup-<phiên bản>-cpu-p4....exe` và `SHA256SUMS.txt` từ [GitHub Releases chính thức](https://github.com/kidlost1412/KidTE/releases), rồi chạy bộ cài. **Đóng Kid TE trước** (nếu đang LIVE thì đợi hết phiên): bộ cài/bộ gỡ thấy Kid TE đang chạy sẽ dừng lại chờ anh tự đóng, không tự tắt app.
2. **Windows hiện màn hình xanh "Windows protected your PC" (SmartScreen)**: bấm
   **More info → Run anyway**. Việc này bình thường với phần mềm nội bộ chưa mua chữ ký
   số (xem cuối trang). Muốn chắc file không bị sửa: đối chiếu mã SHA-256 trong file
   `SHA256SUMS.txt` cùng release (PowerShell:
   `Get-FileHash .\KidTE-Setup-*.exe`).
3. **Chọn thư mục cài** — mặc định là thư mục đã cài trước đó, nếu chưa có thì `D:\KidTE` khi D là ổ đĩa cố định (không phải ổ Windows, CD/DVD hoặc ổ tháo rời), không thì
   `%LOCALAPPDATA%\KidTE`. Không cài vào Program Files (bộ cài từ chối). Bộ cài **cảnh báo** khi chọn
   thư mục trong OneDrive, Desktop/Documents (thường bị OneDrive đồng bộ), ổ mạng, USB/thẻ nhớ, hoặc ổ
   còn dưới 10 GB trống — những chỗ này dễ làm hỏng dữ liệu khách hàng; nên chọn thư mục khác.
4. Mở Kid TE và đợi phần tự tải gói tính năng (xem trên). Dán khoá DeepSeek ở **Cài đặt** nếu dùng AI; cài tiện ích Chrome theo
   `CAI-DAT-TIEN-ICH-CHROME.md`.

## Khi Kid TE không mở được

- **Hộp thoại "Kid TE không khởi động được"**: đọc dòng hướng dẫn trong hộp thoại; bấm **Yes** để mở thư mục chứa
  `launcher-startup.log` (trong `App`). Gửi tệp đó cho hỗ trợ nếu vẫn lỗi.
- **Cổng 8793 bị chiếm**: hộp thoại nêu tên chương trình đang giữ cổng. Đóng chương trình đó, hoặc khởi động lại máy rồi mở Kid TE trước.
- **Không thấy cửa sổ, chỉ mở trình duyệt**: WebView2 bị thiếu/hỏng. Cài lại WebView2 từ
  https://go.microsoft.com/fwlink/p/?LinkId=2124703 rồi tắt hẳn và mở lại Kid TE. Trong lúc đó vẫn dùng được Kid TE qua trình duyệt tại `http://127.0.0.1:8793/app`.
- **Kid TE tự tắt bất thường**: app tự mở lại tối đa 3 lần trong 10 phút; quá số đó sẽ báo lỗi. Hãy khởi động lại máy; nếu lặp lại, vào **Chẩn đoán → Tạo gói hỗ trợ** và gửi tệp zip (gói đã che khoá API, số điện thoại, email và tên người dùng Windows; không tự gửi đi đâu).
- **Cập nhật xong bị quay về bản cũ**: bản mới khởi động lỗi hoặc quá lâu nên Kid TE tự quay về bản trước (dữ liệu không đổi); trang **Cập nhật** ghi lý do. Có thể bấm cập nhật lại; nếu lần trước do khởi động quá lâu, lần thử lại được chờ lâu hơn.
- **Phần mềm diệt virus** chặn/cách ly `KidTE.exe` hoặc `KidTELauncher.exe`: khôi phục tệp và thêm thư mục cài Kid TE vào danh sách loại trừ; nếu tệp bị xoá, chạy lại bộ cài vào đúng thư mục cũ (dữ liệu giữ nguyên).
- **Mất điện khi đang mở app**: lần mở sau Kid TE tự dọn tệp khoá cũ; không cần xoá tay.

## Mọi thứ nằm trong MỘT thư mục

    D:\KidTE\            (thư mục anh chọn lúc cài)
      App\                chương trình — được thay mới mỗi lần cập nhật
      DuLieu\             dữ liệu — KHÔNG bao giờ bị bộ cài ghi đè
        .env              khoá DeepSeek, cài đặt
        data\             khách hàng, Hộp thư, Trí nhớ AI, hồ sơ sản phẩm, lịch ghim, video
        models\           model chép lời đã tải
        captcha\          key (DPAPI), hàng chờ video và log của solver CAPTCHA
        backups\          bản sao lưu
        logs\             nhật ký + luồng bình luận từng phiên LIVE

- **Cập nhật bản mới**: trong app, **Cài đặt → Cập nhật → Cập nhật → Khởi động lại ngay** (không làm khi đang LIVE). Chạy bộ cài mới cũng được: nó tự nhận đúng thư mục cũ, chỉ thay `App`.
- **Đổi từ Live Re sang Kid TE**: đóng ứng dụng sau phiên LIVE rồi chạy bộ cài Kid TE.
  Bộ cài dùng chung mã nâng cấp, tìm thư mục `DuLieu` hiện có và giữ nguyên dữ liệu.
  Nếu bản ghi cài đặt Windows trỏ tới thư mục đã bị xoá, hãy nhìn lại đường dẫn
  được chọn ở bước cài: chọn thư mục **cha** đang chứa `DuLieu` (ví dụ
  `D:\LiveRe`), không chọn chính thư mục `DuLieu`. Tên đường dẫn cũ có thể được
  giữ để ứng dụng đọc tiếp dữ liệu; tên ứng dụng, EXE và biểu tượng sẽ là Kid TE.
- **Nếu trước đây chạy bản mã nguồn**: dữ liệu đang dùng nằm
  trong `backend\data`, `backend\logs`, `backend\models` và `backend\.env` của
  bản mã nguồn. Bộ cài không tự lấy dữ liệu này; thư mục `DuLieu` của một bản cài
  cũ có thể chứa dữ liệu khác và cũ hơn. Sau khi dừng hẳn ứng dụng, hãy sao lưu
  cả hai nơi và đối chiếu dữ liệu trước khi chuyển sang chạy bản cài. Không chép
  đè các tệp SQLite khi ứng dụng còn mở.
- **Gỡ cài**: Settings → Apps → Kid TE → Uninstall. Bộ gỡ hỏi có xoá luôn `DuLieu`
  không; mặc định là **giữ** (cài lại vào đúng thư mục đó sẽ dùng tiếp dữ liệu cũ).
- **Sao lưu tay / chuyển ổ**: tắt Kid TE rồi chép nguyên thư mục.

## Máy mới là máy TRẮNG

Bộ cài không mang dữ liệu riêng của máy khác: không khách hàng, lịch sử hội thoại,
Trí nhớ AI riêng, thông tin shop hay khoá DeepSeek. App có gói hồ sơ sản phẩm chính thức
đóng sẵn; lần đầu cài gói này khi chưa có kho hồ sơ và không đang LIVE, không ghi đè
kho đã có. Mở lần đầu,
người dùng tự làm theo thứ tự (trang **Chẩn đoán** liệt kê đủ và gợi ý cách sửa từng mục):

1. **Cài đặt**: dán khoá DeepSeek; điền **Thông tin shop** (tên, địa chỉ, hotline, giao hàng).
2. **Cài đặt → Cập nhật**: kiểm các gói tính năng đã tự tải xong (đóng/mở lại app khi được nhắc); sau đó ở **Chép lời**, tải model được đánh dấu "Hợp với máy này".
3. Cài tiện ích Chrome rồi mở TikTok LIVE Manager; tiện ích tự đọc giỏ hàng.
4. **Sản phẩm**: kiểm hồ sơ chính thức và liên kết với sản phẩm trong giỏ. Với sản phẩm
   chưa có hồ sơ, bấm **Tạo hồ sơ cho sản phẩm này**, điền công dụng, cách dùng, liều lượng
   và lưu ý; kiểm tra nội dung trước khi duyệt.
5. **Trí nhớ AI → Tài liệu**: tải lên tài liệu công ty, catalogue, bài kỹ thuật canh tác;
   bấm **AI đọc & rút kiến thức** rồi duyệt ở tab Kiến thức.
6. Trong LIVE: sửa câu AI trả lời chưa đúng ở Hộp thư. Chạy học/rút kiến thức và duyệt
   trong Trí nhớ AI khi cần; tự học lại mặc định tắt. Lựa chọn tự kích hoạt kiến thức
   là cài đặt riêng, không đồng nghĩa tự học lại.

Chi tiết mã, dữ liệu và cài đặt thực tế của máy vận hành nằm trong tài liệu kiến thức nội bộ của người vận hành (không nằm trong kho công khai vì mô tả cấu hình thật). Hướng dẫn cài đặt này cũng được đưa vào bộ cài.

**Card đồ hoạ**: gói CUDA không nằm trong bộ cài lõi. Nếu có NVIDIA và muốn tăng tốc chép lời, cài **Tăng tốc chép lời NVIDIA** ở Cập nhật rồi mở lại app; worker và driver cũng phải tương thích. Khi GPU chưa sẵn sàng, chép lời dùng CPU và trang Chép lời báo tình trạng thực tế.

## Về chữ ký số (SmartScreen)

Bộ cài và các tệp `.exe` của Kid TE hiện chưa ký số (Get-AuthenticodeSignature báo NotSigned).
Mức phí và điều kiện cấp chứng chỉ phải kiểm tại thời điểm phát hành; không dùng
ước lượng cũ làm giá hiện hành. Với vài máy trong nội bộ shop, **quyết định hiện tại là chưa ký**: bấm
"Run anyway" một lần mỗi máy + đối chiếu SHA-256 là đủ. Khi phát hành rộng cho khách
ngoài thì nên mua chứng chỉ; lúc đó chỉ cần thêm bước `signtool sign` sau Inno Setup
trong `packaging/build.ps1` (rồi mới tính SHA-256).

> Ghi chú kỹ thuật: tiện ích Chrome/Shopee và các công cụ nội bộ đang gọi thẳng `http://127.0.0.1:8793` (cổng cố định, chưa cấu hình được); vì vậy khi cổng bị chiếm phải đóng chương trình đang giữ cổng.
