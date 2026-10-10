# Cập nhật Kid TE qua GitHub

Kid TE tải các gói phát hành công khai từ `kidlost1412/KidTE`, không cần tài khoản hoặc token trên máy người dùng. Bộ cài mặc định chạy CPU; gói NVIDIA chỉ được tải khi người dùng chọn. Model Whisper và dữ liệu trong `DuLieu` tiếp tục được giữ riêng.

## Dùng trên máy đã cài

Mở **Cài đặt và hệ thống → Cập nhật → Kiểm tra cập nhật**. Chọn kênh ổn định, chọn ứng dụng hoặc tăng tốc NVIDIA, rồi xem tổng dung lượng tải cùng các phụ thuộc trước khi xác nhận. Tải gián đoạn có thể tiếp tục khi thử lại. Gói chỉ được đưa vào thư mục phiên bản sau khi kiểm chữ ký danh mục, hash archive và từng file.

Sau khi chuẩn bị xong, kết thúc phiên LIVE, đóng Kid TE và mở lại bằng biểu tượng Kid TE. Biểu tượng chạy `App/KidTELauncher.exe`; launcher chọn bộ chương trình đã kiểm, truyền đúng đường dẫn dữ liệu và chỉ xác nhận cập nhật khi lõi mới phản hồi đúng PID và mã phiên khởi động. Nếu bản đang chờ không mở được, launcher quay lại bộ trước. Không chép hoặc ghi đè DLL đang chạy.

**Hủy cập nhật chờ** hủy lựa chọn chưa áp dụng. **Quay lại bản trước** chuẩn bị lựa chọn cũ cho lần mở tiếp theo. Các gói đã lưu được giữ để khôi phục; thao tác này không xóa dữ liệu, model hoặc lịch sử khách.

Đợt này hỗ trợ lõi cùng API và schema dữ liệu đã ghim, cùng GPU CUDA 12 cho Windows x64, Python ABI `cp311`, CTranslate2 `4.8.2`, cuDNN 9. Catalog khai thay đổi schema hoặc migration bị chặn. CAPTCHA, FFmpeg, video-sync và processor vẫn phát hành cùng lõi. Tiện ích Chrome dùng đường dẫn ổn định trong `App/extension` và `App/extension-shopee`; cập nhật riêng tiện ích thuộc đợt sau.

## Build và phiên bản

Sau khi bắt đầu lịch sử Git mới, dùng số phát hành rõ ràng để không làm phiên bản giảm về số commit nhỏ:

```powershell
$env:KIDTE_CAPTCHA_SOLVER_ROOT = '<nguồn solver đã kiểm toán>'
.\packaging\build.ps1 -Profile CPU -ReleaseVersion 1.14.254 `
  -Python '<Python của môi trường CPU đã ghim>' `
  -CaptchaPython '<Python của môi trường solver đã kiểm>' -KeepBuild
```

Môi trường chạy toàn suite phải có pytest và Node. Nếu dùng môi trường runtime chỉ để freeze, chạy `backend/tools/run_tests.py --suite all` bằng môi trường phát triển trước, rồi truyền `-SkipTests` vào lệnh build. Không bỏ bước kiểm; không cài dependencies CAPTCHA vào môi trường CPU.

## Danh mục và khóa ký

Catalog là envelope schema 2 có `key_id`, đối tượng `signed` và chữ ký Ed25519. Chữ ký bao phủ JSON UTF-8 đã canonicalize với key được sắp, separator không có khoảng trắng và không có NaN. Public key ghim trong `backend/src/update_trust.py`; app không tải khóa tin cậy từ cùng đường dẫn catalog.

Khóa riêng được lưu ngoài checkout và bảo vệ bằng Windows DPAPI cho tài khoản phát hành. Không đưa khóa, token GitHub hoặc file `.env` lên Git. Bảo quản tài khoản/máy và bản sao khôi phục khóa trước khi đổi máy. File DPAPI không phải khóa dùng được tự do trên một tài khoản Windows khác. Nếu mất khóa, cần phát hành bản app ghim khóa mới; đợt này chưa có cập nhật khóa tin cậy qua mạng.

```powershell
python packaging/update_release.py keygen `
  --private-key '<ngoài checkout>/release-key.dpapi' `
  --public-key '<ngoài checkout>/release-public.json'
python packaging/update_release.py package dist/KidTE --component core --out core.zip
python packaging/update_release.py sign catalog-payload.json `
  --private-key '<ngoài checkout>/release-key.dpapi' --out kidte-catalog-v2.json
```

`keygen` từ chối ghi đè khóa có sẵn. Public key mới phải được ghim và build lại app trước khi dùng catalog tương ứng. Packer lõi kiểm policy CPU, loại launcher ổn định và manifest installer, băm từng file trong lúc ghi và kiểm ZIP trước khi replace output. Công cụ GPU P1 giữ allowlist DLL và license riêng.

Mỗi package record ghi ID, kind, SemVer, URL asset cố định, dung lượng nén/giải nén, hash archive/file, dependency ranges, entrypoint, source commit, API và compatibility. Nâng `sequence` cho mỗi catalog phát hành trên cả hai kênh; không tái sử dụng số cũ. `issued_at`/`expires_at` là UTC; app từ chối catalog hết hạn hoặc thấp hơn sequence đã xác minh. Rollback bộ đã cài không giảm sequence này.

## Bố cục GitHub Releases

Tạo release theo tag phiên bản, tải core ZIP, GPU ZIP cần thiết, installer và checksums đã nghiệm thu. Catalog có thể tham chiếu lại GPU ZIP ở tag cũ để bản vá lõi không bắt tải lại GPU. Mỗi asset phải dưới 2 GiB. [Giới hạn GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).

Catalog ổn định nằm ở asset `kidte-catalog-v2.json` của tag `updates-stable`; kênh thử ở `updates-beta`. Catalog cần trỏ tới asset đã upload đầy đủ trước khi công bố. Mỗi lần cập nhật asset catalog phải ký payload mới với sequence lớn hơn; đồng thời cập nhật thời hạn. ETag và thời gian chờ sau lỗi giảm request thừa; app chỉ kiểm khi người dùng yêu cầu. Nguồn package là asset cụ thể, không phải ZIP source do GitHub tự tạo. [API Releases công khai](https://docs.github.com/en/rest/releases/releases).

Installer và core ZIP mới phải được build từ commit nguồn chốt. Catalog ký đúng SHA256 đo từ artifact thực; việc ký không thay nghiệm thu máy Windows sạch, cài/nâng cấp, WebView và phiên LIVE.

## Dữ liệu và phục hồi

`App/packages/<id>/<version>` chứa gói; `.staging` chứa tải dở; `active.json`, `pending.json`, `previous.json` và `update-journal.json` thuộc metadata chương trình. Điểm chuyển phiên bản là replace atomic của mapping. Khóa hệ điều hành ngăn hai updater chuyển metadata đồng thời. Nhật ký không hoàn tất sẽ đưa về bộ trước; nhật ký cũ không xóa pending của lần cập nhật tiếp theo.

`DuLieu` nằm ngoài package. Launcher không mở hoặc migrate DB trước khi khởi động lõi. Cơ chế rollback này dành cho cập nhật cùng schema đã ghim, không phải rollback dữ liệu. Bản lõi có schema khác phải có quy trình migration và phục hồi được nghiệm thu riêng trước khi catalog được cho phép.
