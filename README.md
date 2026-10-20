# Kid TE

Ứng dụng Windows hỗ trợ vận hành TikTok LIVE và Shopee LIVE: trả lời bình luận bằng DeepSeek, quản lý sản phẩm và khách hàng trên máy, ghim theo lịch video, đồng bộ âm thanh và chép lời. Backend cục bộ mặc định ở `http://127.0.0.1:8793/app`.

Kho công khai chứa mã nguồn, tài nguyên chương trình và fixture kiểm thử. Dữ liệu shop, DB, video, model đã tải, log và khóa truy cập nằm ngoài kho này. Bộ hồ sơ đã chốt trong `backend/prompts/product_defaults.json.gz` (gzip, nội dung JSON không đổi; vẫn đọc được bản `.json` thường) gồm 38 hồ sơ, 3 hồ sơ nguồn tham chiếu và 41 ảnh. Máy mới chọn dùng bộ này hoặc tự thêm; gói kiến thức website `knowledge_seed.json` vẫn có thể nạp thủ công.

## Cài và cập nhật

Nhận installer ở [GitHub Releases](https://github.com/kidlost1412/KidTE/releases), đối chiếu SHA256 đi kèm, rồi cài theo [hướng dẫn Windows](docs/CAI-DAT-WINDOWS.md). Bộ cài mặc định dùng CPU. Tăng tốc NVIDIA là gói tùy chọn; model Whisper được tải riêng khi chọn.

Mở bằng biểu tượng Kid TE để launcher chọn đúng phiên bản. **Cài đặt và hệ thống → Cập nhật** kiểm catalog ký số, báo tổng tải và chuẩn bị gói cho lần mở tiếp theo. Kết thúc LIVE, đóng và mở lại để áp dụng. Dữ liệu tiếp tục nằm trong `DuLieu` cạnh `App`.

Từ P3, bộ cài chỉ mang lõi và các processor nhỏ. Công cụ video, CAPTCHA/monitor, đồng bộ video, worker chép lời và GPU là các gói tải theo nhu cầu. Model và dữ liệu giữ riêng. Processor nghiệp vụ vẫn cập nhật cùng lõi cho tới đợt hợp đồng plugin tiếp theo. [Cơ chế cập nhật và phát hành](docs/CAP-NHAT-GITHUB.md).

Bản 1.15.21 P4 cập nhật app và tiện ích qua kênh Thử nghiệm, dùng lại runtime 1.0.0. Máy mới cài bộ P4 rồi kiểm tra cập nhật trước khi nhập dữ liệu. [Thay đổi 1.15.21](docs/THAY-DOI-1.15.21.md).

## Phát triển

Python 3.11 x64 trên Windows. Môi trường runtime CPU dùng `backend/requirements-cpu.lock.txt`; môi trường kiểm thử bổ sung `backend/requirements-dev.txt`. Test giao diện và tiện ích cần Node.js. Tránh dùng dữ liệu vận hành để chạy thử; chọn thư mục riêng bằng `KID_TE_ROOT` và cổng riêng bằng `CHAT_PORT`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-cpu.lock.txt -r backend/requirements-dev.txt
.\.venv\Scripts\python.exe backend/tools/run_tests.py --suite all
$env:KID_TE_ROOT = '<thư mục dữ liệu thử mới>'
.\.venv\Scripts\python.exe backend/src/app_entry.py --host 127.0.0.1 --port 8794 --safe-mode --no-ui
```

Kiểm dựa vào dữ liệu của máy vận hành sẽ skip khi dữ liệu đó không có trong checkout công khai; test nghiệp vụ còn lại tạo dữ liệu tạm hoặc dùng fixture. Không chép DB shop vào Git để tránh skip.

## Đóng gói

Build cần PyInstaller, Inno Setup và FFmpeg/FFprobe. Core dùng môi trường CPU riêng; CAPTCHA dùng môi trường và nguồn solver đã kiểm toán riêng. Bundle guard trong `packaging/solver-patches` giữ hash/provenance và kiểm mã solver; không bỏ guard để build với nguồn khác. [Ranh giới CAPTCHA](docs/TICH-HOP-CAPTCHA.md).

Dùng `packaging/build.ps1 -Profile CPU -ReleaseVersion <x.y.z> -Python <CPU Python>` để tạo lõi. Thêm `-BuildComponents -CaptchaPython <solver Python>` để tạo các cây package riêng trong build. Số phát hành rõ ràng giúp giữ thứ tự phiên bản khi lịch sử kho bắt đầu lại; không dùng số commit mới để hạ phiên bản. [Build CPU và gói GPU](docs/GOI-GPU-TUY-CHON.md), [ký catalog và phát hành](docs/CAP-NHAT-GITHUB.md).

## Thư mục

| Đường dẫn | Nội dung |
|---|---|
| `backend/src`, `backend/prompts` | Backend, giao diện và hồ sơ mặc định |
| `backend/tests`, `backend/tools` | Test và công cụ phát triển |
| `plugins` | Processor nghiệp vụ và dịch vụ đồng bộ video |
| `extension`, `extension-shopee` | Tiện ích Chrome theo sàn |
| `packaging` | Build, installer, guard và công cụ phát hành |
| `ui`, `assets` | Theme, tài nguyên và biểu tượng |
| `docs` | Hướng dẫn vận hành và schema baseline |

Tiện ích Chrome dùng đường dẫn ổn định và được nạp bằng Chrome theo [hướng dẫn tiện ích](docs/CAI-DAT-TIEN-ICH-CHROME.md). Cập nhật core chưa tự thay thế bước Reload tiện ích trong Chrome.
