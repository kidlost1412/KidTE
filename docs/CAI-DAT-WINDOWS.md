# Cài Kid TE trên Windows

Cần Windows 10/11 64-bit. Không cần quyền quản trị.

Với bản 1.14.257, dùng [bộ cài P3 r1](https://github.com/kidlost1412/KidTE/releases/tag/v1.14.257-p3-r1). Bộ cài 257 ban đầu bị lỗi đọc kết quả WebView2 và chưa đạt nghiệm thu cài mới.

Từ bộ cài 1.14.257 r1, trình cài kiểm tra Microsoft Edge WebView2 trước khi cài Kid TE. Nếu máy chưa có runtime này, trình cài tải bootstrapper từ Microsoft, xác minh chữ ký rồi cài bằng quyền người dùng hiện tại; lần này cần kết nối mạng. Máy đã có WebView2 không tải lại. Khi tải/cài runtime thất bại, trình cài báo bước lỗi và dừng để anh xử lý rồi chạy lại. Muốn cài ngoại tuyến, cài trước **Evergreen Standalone Installer x64** từ [trang WebView2 của Microsoft](https://developer.microsoft.com/en-us/microsoft-edge/webview2/). Runtime được Windows quản lý riêng, không nằm trong thư mục Kid TE. Dung lượng bộ cài không bao gồm runtime tải riêng khi máy thiếu.

P3 cài lõi nhỏ trước. Công cụ video, giải CAPTCHA/monitor, đồng bộ video và bộ chép lời được tải riêng ở **Cài đặt → Cập nhật**; NVIDIA và model cũng tải riêng. Bản thử dùng kênh **Thử nghiệm**. Khi nâng từ P2, cài installer P3 một lần để nâng launcher rồi mới tải các tính năng này.

Trình giải CAPTCHA nằm trong `App\packages\captcha-solver\<phiên bản>\Captcha`.
Mở Kid TE → **Giải CAPTCHA** để nhập SadCaptcha key. Windows sẽ hỏi quyền
Administrator khi khởi chạy solver; Kid TE không lấy quyền đó cho các chức năng
khác. Chỉ một solver được chạy trong mỗi phiên Windows. Nếu key hết lượt,
solver dừng và chờ key mới. Kết quả kéo phải được xác nhận trong TikTok LIVE
Studio; thông báo hộp thoại biến mất chưa đủ để kết luận giải thành công.
Nếu cần nhận cảnh báo và video, nhập bot token cùng Chat ID dạng số trên cùng
trang rồi khởi động lại solver. Key và token cần nhập lại trên máy Windows mới.

## Cài đặt

1. Tải bộ cài CPU và `SHA256SUMS.txt` từ [GitHub Releases chính thức](https://github.com/kidlost1412/KidTE/releases), rồi chạy bộ cài. Với 1.14.257, tên file là `KidTE-Setup-1.14.257-cpu-p3-r1.exe`. Bản P3 hiện là bản thử nghiệm để nghiệm thu từng đợt.
2. **Windows hiện màn hình xanh "Windows protected your PC" (SmartScreen)**: bấm
   **More info → Run anyway**. Việc này bình thường với phần mềm nội bộ chưa mua chữ ký
   số (xem cuối trang). Muốn chắc file không bị sửa: đối chiếu mã SHA-256 trong file
   `SHA256SUMS.txt` cùng release (PowerShell:
   `Get-FileHash .\KidTE-Setup-*.exe`).
3. **Chọn thư mục cài** — mặc định là thư mục đã cài trước đó, nếu chưa có thì `D:\KidTE` khi D là ổ đĩa cố định (không phải ổ Windows, CD/DVD hoặc ổ tháo rời), không thì
   `%LOCALAPPDATA%\KidTE`. Nên chọn ổ còn nhiều chỗ trống. Không cài vào Program Files
   (bộ cài sẽ từ chối, vì app cần ghi dữ liệu ngay cạnh chương trình).
4. Mở Kid TE → **Cài đặt → Cập nhật → Thử nghiệm**: cài **Bộ chép lời trên máy** nếu cần phân tích video; **Công cụ video** được chọn tự động theo phụ thuộc. Đóng và mở lại app để áp dụng. Sau đó ở **Chép lời**, tải model được đánh dấu "Hợp với máy này" (một lần, cần mạng). Dán khoá DeepSeek ở **Cài đặt** nếu dùng AI; cài tiện ích Chrome theo
   `CAI-DAT-TIEN-ICH-CHROME.md`.

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

- **Cập nhật bản mới**: chạy bộ cài mới — nó tự nhận đúng thư mục cũ, chỉ thay `App`.
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
2. **Cài đặt → Cập nhật → Thử nghiệm**: chọn những gói tính năng cần dùng, đóng/mở lại app để áp dụng; sau đó ở **Chép lời**, tải model được đánh dấu "Hợp với máy này".
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

Kiểm bộ cài1.14.240 bằng Get-AuthenticodeSignature cho trạng thái NotSigned.
Mức phí và điều kiện cấp chứng chỉ phải kiểm tại thời điểm phát hành; không dùng
ước lượng cũ làm giá hiện hành. Với vài máy trong nội bộ shop, **quyết định hiện tại là chưa ký**: bấm
"Run anyway" một lần mỗi máy + đối chiếu SHA-256 là đủ. Khi phát hành rộng cho khách
ngoài thì nên mua chứng chỉ; lúc đó chỉ cần thêm bước `signtool sign` sau Inno Setup
trong `packaging/build.ps1` (rồi mới tính SHA-256).
