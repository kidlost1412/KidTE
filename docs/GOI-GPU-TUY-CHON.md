# Bộ cài CPU và gói GPU tùy chọn — P1

**Ghi chú P3:** phần dưới lưu mô tả/provenance đợt P1. Bản hiện tại tách cả Whisper CPU, CAPTCHA, FFmpeg và đồng bộ video thành package; xem [hướng dẫn cập nhật hiện tại](CAP-NHAT-GITHUB.md). Build phát hành hiện tại dùng profile CPU và số `ReleaseVersion` rõ ràng. GPU được tải ở Cập nhật, giữ cùng pack đã ký; không build installer GPU gộp kiểu P1.

Phạm vi đợt này: đưa NVIDIA CUDA/cuDNN ra khỏi bộ cài mặc định. Core vẫn chứa runtime Whisper CPU, đồng bộ video, CAPTCHA và FFmpeg như trước. Model và dữ liệu người dùng tiếp tục ở ngoài chương trình. Updater GitHub và nút cài addon trong giao diện thuộc đợt P2; P1 dùng lựa chọn gói GPU rõ ràng qua môi trường của tiến trình.

## Build

Tạo môi trường core riêng bằng Python 3.11 x64, cài `backend/requirements-cpu.lock.txt`, PyInstaller 6.15.0 và pyinstaller-hooks-contrib 2026.7. Lock này ghim phiên bản của runtime Windows, chưa phải lock có hash wheel. Môi trường build CAPTCHA cần dependencies và nguồn solver đã qua kiểm toán; không cài hoặc vá solver để bỏ qua kiểm hash.

Từ gốc checkout phát triển, chạy:

```powershell
& .\.venv\Scripts\python.exe backend\tools\run_tests.py --suite all
if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
.\packaging\build.ps1 -Profile CPU `
  -Python .\.venv\Scripts\python.exe `
  -CaptchaPython D:\TikTokCaptchaSolver-clean-20260926\venv\Scripts\python.exe `
  -SkipTests -KeepBuild
```

`-CaptchaPython` mặc định bằng `-Python` để giữ cách build cũ. Khi dùng môi trường core sạch, phải truyền interpreter của môi trường CAPTCHA đã chuẩn bị. Môi trường dùng để chạy toàn suite cần pytest và Node; có thể chạy runner riêng trước rồi dùng `-SkipTests` cho build với môi trường core chỉ có dependencies đóng gói.

CPU là mặc định cả trong script và spec. `-Profile GPU` là bộ cài kiểu cũ mang DLL GPU bên trong; dùng `backend/requirements-gpu.lock.txt` cho môi trường đó. Không nhầm profile GPU với ZIP addon.

Đầu ra có hậu tố `-cpu` hoặc `-gpu`: installer, release manifest và inventory. Inventory lưu hash từng file, dung lượng trước nén theo nhóm và kích thước installer đo thực tế. Script giữ các artifact cũ. Installer CPU chỉ dọn DLL NVIDIA cũ ở `App/_internal/nvidia` khi nâng cấp; giữ AppId và bố cục `App`/`DuLieu`. Gói ngoài nằm trong `App/components/gpu-cuda12/<version>` nên không thuộc đường dẫn dọn này. Đóng Kid TE trước khi cài/nâng cấp.

## Tạo gói GPU

Chỉ tạo từ bộ dependencies đã kiểm, không từ ZIP source của GitHub. Ví dụ cho môi trường NVIDIA đã dùng trong bản 249:

```powershell
.\.venv\Scripts\python.exe packaging\gpu_pack.py build `
  <moi-truong-NVIDIA-da-kiem>\Lib\site-packages `
  PhatHanh\gpu-cuda12-1.0.0.zip `
  --version 1.0.0 --python-abi cp311 `
  --source-commit 2d23e544c0e29f9a4cf9d39d52316fe022e6c417 `
  > PhatHanh\gpu-cuda12-1.0.0.checksums.json
```

Thay commit bằng commit nguồn thực tế của lần tạo gói. Packer đọc DLL theo từng khối, kiểm file không đổi trong lúc ghi, giữ license của từng distribution, xác minh ZIP rồi mới replace file đầu ra. Danh sách DLL cố định theo baseline đã kiểm; nâng phiên bản NVIDIA có tên DLL khác phải cập nhật và nghiệm thu danh sách trước.

ZIP chứa `gpu-runtime.json`, `nvidia/<package>/bin/*.dll` và `licenses/**`. Manifest ghi hash/kích thước từng file, commit nguồn, RECORD của distribution, Python ABI, CTranslate2, CUDA và cuDNN. Gói hiện tại dành cho Windows x64, CPython 3.11, CTranslate2 4.8.2, CUDA 12/cuDNN 9.

## Chọn gói GPU trên máy dùng app

Nhận SHA256 của ZIP và manifest từ người phát hành đã xác minh. So hash ZIP với giá trị đó trước khi giải nén. Giải nén vào thư mục mới, có tên cuối trùng phiên bản manifest, chẳng hạn `App/components/gpu-cuda12/1.0.0`; không chép DLL vào `_internal` hoặc `DuLieu`.

Mở Kid TE từ PowerShell với hai biến chỉ áp dụng cho tiến trình được mở:

```powershell
$env:KIDTE_GPU_RUNTIME_ROOT = 'D:\KidTE\App\components\gpu-cuda12\1.0.0'
$env:KIDTE_GPU_RUNTIME_SHA256 = '<SHA256 manifest do nguoi phat hanh cung cap>'
& 'D:\KidTE\App\KidTE.exe'
```

Không lấy hash của một manifest chưa được xác minh rồi tự coi đó là nguồn tin cậy. P1 có kiểm integrity bằng digest do người vận hành cung cấp; chưa có chữ ký người phát hành hoặc cập nhật mạng tự động. P2 sẽ dùng catalog ký số và kho GitHub công khai không yêu cầu đăng nhập theo lựa chọn của chủ dự án.

Runtime chỉ kiểm toàn gói khi cần GPU lần đầu trong tiến trình, không băm 2 GB ở mỗi lần mở app. Sai hash, thiếu/thừa file, sai phiên bản CTranslate2/ABI hoặc đường dẫn không hợp lệ thì chuyển sang CPU. Lỗi CUDA khi chép lời vẫn có đường lui CPU hiện hữu. Đổi/gỡ gói sau khi đã nạp DLL phải đóng và mở lại app; không trộn hai runtime trong một tiến trình. Không thay lựa chọn model.

Để dùng CPU, mở một tiến trình mới không có hai biến trên. Không tự tải lại model và không xóa model custom/legacy.

## Kiểm bản EXE

`packaging/smoke_gpu_runtime.py` chạy HTTP trên cổng localhost riêng, tạo dữ liệu mới và upload video fixture ngắn. Nó kiểm health, HTML, asset, model đã chọn và device thực ghi trong metadata của transcript. Không gọi model tư vấn hoặc sàn thật, không dùng DB shop, không xuất nội dung transcript vào báo cáo.

```powershell
.\.venv\Scripts\python.exe packaging\smoke_gpu_runtime.py `
  --app-dir .\dist\KidTE `
  --model '<thu muc model local da co>' --audio '<fixture WAV PCM16>' `
  --work-dir '<thu muc thu moi chua ton tai>' --expected-device cpu
```

Với GPU thêm `--expected-device cuda --gpu-root <absolute path> --gpu-manifest-sha <digest>`. Với gói sai hash thêm `--expect-gpu-rejection`, chọn `--expected-device cpu` và digest sai có chủ ý. Không chạy smoke đồng thời với build thay cùng thư mục `dist`.

Một smoke trên máy phát triển không thay kiểm máy Windows sạch, wizard nâng cấp từ 249 hoặc nghiệm thu phiên LIVE thật. Các mục đó phải có biên bản trước phát hành cho người dùng.
