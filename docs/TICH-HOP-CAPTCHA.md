# Tích hợp CAPTCHA độc lập

## Ranh giới

- Solver 0.1.0 (mã nguồn mặc định tìm ở `TikTokCaptchaSolver-clean-20260926` nằm hai cấp trên thư mục gốc mã nguồn, hoặc đặt `KIDTE_CAPTCHA_SOLVER_ROOT`) xử lý toàn bộ ảnh,
  SadCaptcha và chuột. Kid TE không import hoặc monkey-patch thuật toán đó.
- Một tiến trình riêng chạy trong cùng phiên desktop Windows và có quyền Admin.
  Đóng Kid TE kết thúc solver, monitor và các tiến trình con do app quản lý.
  Mở Kid TE nhiều lần không tạo solver thứ hai.
  Không chạy thêm BAT/PowerShell standalone cũ trong lúc solver Kid TE đang chạy;
  hai chương trình cùng điều khiển chuột sẽ xung đột.
- Mã nguồn dùng venv riêng của solver. Bản phát hành đóng gói một EXE solver
  riêng trong `App\Captcha`; không dùng runtime của EXE Kid TE.
- Key chỉ lưu dưới dạng DPAPI của người dùng Windows trong `DuLieu\captcha`.
  API không trả key; log không chứa key. HTTP 401/403 dừng solver cho tới khi
  người dùng lưu key mới. Chuyển sang máy hoặc tài khoản Windows khác phải nhập
  lại key và Telegram vì DPAPI không giải mã được hộ người dùng khác.
- Telegram cần Chat ID của cuộc trò chuyện nhận tin, không phải số ở đầu bot
  token. Trang Giải CAPTCHA cho gửi tin thử và cho phép sửa riêng Chat ID mà
  không cần nhập lại token. Sau khi lưu, giám sát bắt đầu ngay nếu solver đã chạy.
- Trang trạng thái lấy PID và quyền Admin từ worker thực tế. Nếu Windows giấu
  command line của tiến trình nâng quyền, file `solver-session.json` được đối
  chiếu với PID, thời điểm tạo tiến trình và tên executable. Các phiên Kid TE
  dùng thư mục dữ liệu khác được coi là solver bên ngoài, không chạy trùng.
- Dấu từ chối key gắn với SHA-256 của key đã bị từ chối; key mới không bị dấu
  của tiến trình cũ chặn khởi chạy. Solver vẫn cần khởi động lại để nạp key mới.
- Kid TE đọc tiến trình và nhật ký native để hiển thị trạng thái. Dòng
  `dragged` chỉ chứng minh đã phát lệnh kéo; `no longer visible` không chứng
  minh TikTok chấp nhận. Không ghi là giải thành công nếu thiếu xác nhận.

## Thứ tự kiểm tra

1. Kiểm tra đơn vị: DPAPI, chọn executable, phát hiện tiến trình trùng,
   đọc log, xử lý key sai. Không cần ảnh hay gọi SadCaptcha thật.
2. Chạy bản mã nguồn: khởi động/đóng Kid TE nhiều lần; solver và monitor phải dừng theo app,
   cổng 8793 không đổi và log vẫn tăng. Thử từ chối UAC.
3. Đóng gói: EXE solver riêng, thư mục dữ liệu trắng, không có key, ảnh hoặc
   log của máy phát triển. Cài thử vào thư mục tạm.
4. Khi có key hợp lệ và LIVE thật: quay toàn màn hình, đối chiếu ảnh được gửi,
   phản hồi API, tọa độ, chuột và kết quả trong Studio. Kiểm cả puzzle giao diện
   tối/cũ, shapes, tỷ lệ màn hình, HTTP 400, 401 và mất mạng.

Không đưa bước 4 vào tuyên bố nghiệm thu nếu chưa có key và CAPTCHA thật.
Video tự động quay vùng cửa sổ TikTok Studio trong 30 giây, không quay chồng.
Capture dùng tọa độ vật lý trong màn hình ảo, không cố định màn hình chính.
File quay xong được đưa vào hàng chờ SQLite; retry sau 30 giây, 2 phút,
10 phút, rồi mỗi 30 phút. Chỉ xóa khi HTTP thành công và JSON `ok:true`.
Video chưa gửi quá 30 phút thì bị bỏ, không thử lại nữa; bản ghi đã gửi/xóa và
file quay dở được dọn sau 7 ngày (file quay dở chỉ khi mutex recorder rảnh).
Kho 5 GB, thiếu khoảng trống thì dừng quay, solver vẫn chạy.
Khi solver đang tắt, khởi chạy lại để tiếp tục hàng chờ. UI hiển thị số chờ,
đang gửi và cảnh báo kho đầy. Các mô phỏng không thay thế nghiệm thu tọa độ/DPI,
30 giây thực, Telegram và CAPTCHA thật.

Giới hạn thử: mỗi đợt CAPTCHA (kết thúc khi 15 giây không còn thấy CAPTCHA)
solver chỉ thử kéo tối đa 7 lần (`packaging/solver-patches/episode_guard.py`);
sang lần thứ 8 nó dừng tự giải, ghi `CAPTCHA_MANUAL_REQUIRED` và monitor báo
Telegram một lần để chủ shop vào giải tay.

Nhánh legacy thêm kiểm tra challenge sau API trước chuột; nhánh modern/shapes
và hệ số kéo giữ nguyên. Build solver dùng snapshot/hash tại
`packaging/solver-patches/manifest.json`. `KidTECaptcha.spec` kiểm hash nguồn solver
trước đóng gói và từ chối nguồn không khớp; build không tự chạy `install_guard.py`.
Trong đợt bảo trì này solver ngoài và toàn bộ solver-patches chỉ được đọc: không
chạy công cụ vá trên solver đang vận hành. Khi hash lệch, báo rõ sai lệch; không
sửa nguồn hoặc manifest để vượt kiểm tra. Bộ kiến thức trong kho nguồn:
`docs/kien-thuc/05-CAPTCHA.md`.

## Vòng đời khi đóng Kid TE

- Đóng cửa sổ Kid TE sẽ dừng solver, monitor Telegram, tiến trình ghi hình/FFmpeg, dịch vụ đồng bộ video và backend. Thu nhỏ cửa sổ vẫn giữ app chạy. Chrome/OBS và solver ngoài không thuộc Kid TE được giữ nguyên.
- Worker CAPTCHA vẫn là tiến trình riêng nhưng nhận `--owner-pid`, giữ handle của core để tự thoát khi core mất; tín hiệu `stop-requested.marker` cho phép worker quyền Admin tự dừng.
- Supervisor dừng cây tiến trình do nó sở hữu; nếu không dừng sau thời gian chờ, kết thúc cưỡng bức đúng tiến trình đã xác minh. Một lỗi dừng không ngăn dọn các dịch vụ còn lại; lỗi được ghi log.
