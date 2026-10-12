# Cập nhật Kid TE qua GitHub

Kid TE tải các gói phát hành công khai từ `kidlost1412/KidTE`, không cần tài khoản hoặc token trên máy người dùng. Bộ cài mặc định chạy CPU; gói NVIDIA chỉ được tải khi người dùng chọn. Model Whisper và dữ liệu trong `DuLieu` tiếp tục được giữ riêng.

## Dùng trên máy đã cài

Mở **Cài đặt và hệ thống → Cập nhật → Kiểm tra cập nhật**. Chọn kênh ổn định, chọn ứng dụng hoặc tăng tốc NVIDIA, rồi xem tổng dung lượng tải cùng các phụ thuộc trước khi xác nhận. Tải gián đoạn có thể tiếp tục khi thử lại. Gói chỉ được đưa vào thư mục phiên bản sau khi kiểm chữ ký danh mục, hash archive và từng file.

Sau khi chuẩn bị xong, kết thúc phiên LIVE, đóng Kid TE và mở lại bằng biểu tượng Kid TE. Biểu tượng chạy `App/KidTELauncher.exe`; launcher chọn bộ chương trình đã kiểm, truyền đúng đường dẫn dữ liệu và chỉ xác nhận cập nhật khi lõi mới phản hồi đúng PID và mã phiên khởi động. Nếu bản đang chờ không mở được, launcher quay lại bộ trước. Không chép hoặc ghi đè DLL đang chạy.

**Hủy cập nhật chờ** hủy lựa chọn chưa áp dụng. **Quay lại bản trước** chuẩn bị lựa chọn cũ cho lần mở tiếp theo. Các gói đã lưu được giữ để khôi phục; thao tác này không xóa dữ liệu, model hoặc lịch sử khách.

P3 hỗ trợ lõi cùng API/schema dữ liệu đã ghim và các gói tùy chọn dưới đây. Catalog khai thay đổi schema hoặc migration bị chặn. Từ series **P4** lõi được tách thành ba gói (mục "Cập nhật nhỏ: runtime / app / extensions"): sửa một dòng mã chỉ tải gói `app` (dưới 1 MB), không tải lại thư viện nền.

| Gói | Khi cần | Phụ thuộc |
|---|---|---|
| `core` (P3, nguyên khối) | Giao diện, trả lời, quản lý sản phẩm/khách, processor, tiện ích Chrome và thư viện nền trong một ZIP ~48 MB | Không tự kéo tính năng tùy chọn |
| `runtime` (P4) | Python đóng băng + thư viện bên thứ ba (`KidTE.exe`, `_internal`); hiếm khi đổi | Không |
| `app` (P4) | Mã Kid TE (`backend/src`), giao diện web, prompt, plugin nhỏ, tài liệu, `VERSION` | `runtime` cùng ABI (`runtime_api`) |
| `extensions` (P4) | Tiện ích Chrome `extension/` và `extension-shopee/` | Không |
| `media-tools` | Xử lý video, poster, sóng âm và quay cảnh báo CAPTCHA | Lõi (`core`, hoặc `app` khi đã tách) |
| `captcha-solver` | Giải CAPTCHA và monitor Telegram | Lõi, công cụ video |
| `video-sync` | Đồng bộ âm thanh/video LIVE | Lõi |
| `video-analysis-cpu` | Chép lời video trên máy | Lõi, công cụ video, model đã chọn riêng |
| `gpu-cuda12` | Tăng tốc chép lời NVIDIA | Runtime Windows x64/cp311/CTranslate2 4.8.2/cuDNN 9 |

Worker chép lời là EXE có runtime riêng, giao tiếp JSON protocol 1 qua stdin/stdout. Nó chỉ đọc WAV PCM16/16 kHz và model cục bộ được lõi truyền vào, không mở DB hay tải model ngầm. Lõi quản lý job, bản nháp và dữ liệu. Thiếu gói thì chức năng báo cách tải trong Cập nhật; các chức năng còn lại tiếp tục dùng được.

Ví dụ sửa CAPTCHA: build và phát hành phiên bản mới của `captcha-solver`, ký catalog với sequence mới và giữ nguyên record lõi, media-tools, GPU cũ. Máy đã có các phụ thuộc tương thích chỉ tải ZIP CAPTCHA. Với P4, sửa giao diện hoặc processor chỉ tải gói `app`; với P3 nguyên khối vẫn tải core ZIP (chưa có tải delta theo file). Bản vá worker chỉ tải worker ZIP nếu API/phụ thuộc giữ tương thích. Thay native dependency thì resolver trình bày đầy đủ các gói phải đổi trước khi người dùng xác nhận.

**Gỡ tính năng** chuẩn bị mapping bỏ gói cho lần mở lại. Gói đang được tính năng khác dùng không được gỡ trước phụ thuộc. Dữ liệu/model không bị xóa; phiên bản package cũ được dọn tự động theo mục "Dọn dẹp cục bộ" (giữ bản đang chạy và đúng một bản để quay lại).

P2 launcher chỉ nhận core/GPU. Cài installer P3 một lần để nâng launcher; catalog P2 được giữ ở `updates-beta`, còn P3 dùng `updates-beta-p3` hoặc `updates-stable-p3`. Hai feed tránh gửi ID mới cho launcher cũ. Installer chính thức mang `bootstrap-request.json` đã ký: sau kiểm khóa/cổng/LIVE, launcher kiểm file lõi cài sẵn theo inventory, sao chép vào thư mục phiên bản và chuẩn bị mapping mới, giữ GPU đã chọn. Bootstrap cũng qua kiểm readiness/rollback. Không sao chép bằng hardlink, không migrate DB.

Bản vá P3 `1.14.256` giữ nguyên pending nếu chuẩn bị bootstrap thất bại, giữ các lựa chọn cài/gỡ tính năng đã chuẩn bị và từ chối core trong installer thấp hơn core active hoặc pending. Lớp lưu gói kiểm cả tập phụ thuộc trước khi chuẩn bị hoặc áp dụng mapping; một package có inventory đúng nhưng thiếu dependency vẫn bị từ chối.

Catalog tải từ mạng luôn phải còn hạn. Riêng inventory đã ký nằm trong installer được dùng sau ngày hết hạn để cài offline: launcher vẫn kiểm chữ ký ghim, ngày phát hành không ở tương lai, sequence không thấp hơn mốc đã nhận, toàn bộ file lõi khớp và không hạ phiên bản active/pending. Đây chỉ là sử dụng các file đã có trong bộ cài, không cho phép tải gói từ feed hết hạn.

## Cập nhật nhỏ: runtime / app / extensions (series P4)

Trước đây `core` (~48 MB nén, ~116 MB giải nén) gói cả Python + thư viện (ít đổi) với mã Kid TE (~2,4 MB) và tiện ích Chrome (0,34 MB), nên sửa một dòng cũng tải 48 MB. Từ P4 lõi tách thành ba gói, đo từ bản dựng 1.15.0:

| Gói | Nén | Giải nén | File | Đổi khi |
|---|---|---|---|---|
| `runtime` | ~48,5 MB | ~102 MB | ~1.280 | Đổi Python, `requirements`, spec hoặc `runtime_bootstrap.py` |
| `app` | ~0,77 MB | ~2,4 MB | ~170 | Mỗi lần phát hành (mã, giao diện, prompt, plugin, tài liệu) |
| `extensions` | ~0,13 MB | ~0,34 MB | ~33 | Chỉ khi `extension/` hoặc `extension-shopee/` đổi |

**Cách tải mã.** `KidTE.exe` của `runtime` chỉ nhúng `packaging/runtime_bootstrap.py` và thư viện bên thứ ba; mọi module của Kid TE bị loại khỏi PYZ (`KidTE.spec` loại theo đường dẫn `backend/src`, vẫn giữ nguyên phân tích để thư viện bên thứ ba mà mã dùng được gom đủ). Launcher kiểm hash `backend/src/app_entry.py` của gói `app` đang chọn rồi đặt `KIDTE_APP_PACKAGE_DIR`; bootstrap thêm `backend/src` vào `sys.path` (không ghi bytecode vào gói bất biến) và chạy `app_entry.main()`. `config.APP_PACKAGE_ROOT` trỏ vào gói app: `RESOURCE_ROOT` (giao diện web, prompt), `PLUGINS_DIR`, `VERSION`. Không có biến môi trường (bộ cài chưa qua launcher, chạy trực tiếp) thì app nằm cạnh `KidTE.exe`. Gói `app` giao nguyên `.py` (catalog cấm `.pyc`); khởi động ~1,1 giây, không chậm đáng kể.

**Phụ thuộc và ABI.** `app` yêu cầu `runtime >=N.0.0,<N+1.0.0` và khai `compatibility.runtime_api = N`; phiên bản chính của `runtime` phải bằng `runtime_api`. Cả bộ chuyển như một generation nên `runtime` và `app` luôn cùng ABI: `check_generation` (catalog và lớp lưu gói) từ chối thiếu một trong hai hoặc lệch ABI. Muốn đổi runtime sang ABI mới phải phát hành `app` mới cùng catalog; resolver kéo cả cặp. `extensions` độc lập (`extension_api`). `app` cũng khai ghim `data_schema` (như `core`). Các gói tùy chọn vẫn ghi `dependencies.core`, record và ZIP không đổi: trong generation tách, `app` đóng vai `core` cho phụ thuộc này (`update_catalog.provided`).

**Một generation nguyên tử.** `active.json`/`pending.json`/`previous.json` vốn ánh xạ cả bộ gói nên launcher chọn `runtime + app + extensions` cùng lúc, giữ nguyên handshake readiness, journal và rollback: nếu `runtime` mới hay `app` mới lỗi trước khi sẵn sàng, quay về generation trước cho cả ba. Launcher chạy `KidTE.exe` của `runtime` (hoặc `core` kiểu cũ), kiểm hash entrypoint của `app`, và từ chối khởi động nếu hash lệch.

**Tiện ích Chrome ở đường dẫn ổn định.** Chrome "Load unpacked" luôn trỏ `App\extension` và `App\extension-shopee`; đường dẫn này không bao giờ đổi. Trước khi mở app, launcher đồng bộ nội dung hai thư mục từ gói `extensions` đang chọn (`extension_sync.py`): ghi file khác hash (temp rồi replace, `manifest.json` cuối cùng), xóa file thừa, rồi ghi `App\extensions-sync.json`. Gói không đổi thì **không ghi gì** (không đổi mtime), nên không cần nạp lại tiện ích trong Chrome. Khi có thay đổi launcher ghi dòng nhắc vào `launcher-startup.log`; người vận hành bấm nạp lại tiện ích trong `chrome://extensions`. Thư mục là reparse point thì bỏ qua đồng bộ.

**Chuyển từ `core` nguyên khối.** Gói `core` vẫn hợp lệ (feed P3 giữ nguyên cho launcher/app cũ, không gửi ID mới). Danh mục P4 (`updates-<kênh>-p4`) liệt kê `runtime`, `app`, `extensions` và các gói tùy chọn, không còn `core`. Khi bản cài đang chạy `core` yêu cầu `app` (hoặc `runtime`/`extensions`), `UpdateManager` mở rộng thành đủ ba gói, bỏ `core` khỏi mapping mới, chỉ tải phần chưa khớp inventory bản cài (gói khớp byte với file ở `App` được tái dùng, không tải) và giữ nguyên các gói tùy chọn. Generation `core` cũ được giữ làm điểm quay lại. Bộ cài mới mang `bootstrap-request.json` liệt kê ba gói; launcher đăng ký chúng không tải gì.

**RỦI RO KHI CHUYỂN ĐỔI (đọc trước khi phát hành lần đầu).** Launcher (`KidTELauncher.exe`) nằm ngoài các gói và chỉ được thay bằng bộ cài. Launcher cũ không hiểu mapping `runtime/app`, và app 1.14.x không đọc được danh mục P4. Vì vậy: (1) cài bản mới bằng bộ cài mới nhất một lần (giữ `DuLieu`; launcher mới và baseline tách được đăng ký tại chỗ); (2) app mới đặt `KIDTE_LAUNCHER_CAPS=split-components` qua launcher, và `UpdateManager` từ chối (`update_launcher_upgrade_required`) mọi yêu cầu chuyển sang gói tách nếu launcher không khai năng lực này, tránh để máy mắc kẹt. Đừng đưa `runtime/app/extensions` vào feed P3.

**Quy trình phát hành.**

```powershell
# Phát hành đầy đủ (đổi runtime): dựng, đóng ba gói vào PhatHanh\packages-<phiên bản>\
.\packaging\build.ps1 -Profile CPU -ReleaseVersion 1.15.0 -SkipInstaller
# Phát hành thường (chỉ mã/tiện ích): không PyInstaller, không launcher, không cài đặt
.\packaging\build.ps1 -Profile CPU -ReleaseVersion 1.15.1 -AppOnly
# Bộ cài dùng đúng runtime đã phát hành (đúng byte với catalog)
.\packaging\build.ps1 -Profile CPU -ReleaseVersion 1.15.1 -RuntimeZip <KidTE-runtime-1.0.0-p4.zip>
# Danh mục P4 đầu tiên từ danh mục P3 đã đăng
python packaging/build_catalog.py --published p3-catalog.json --out payload.json `
  --remove-component core `
  --add-component runtime runtime.zip <url> 1.0.0 <commit> `
  --add-component app app.zip <url> 1.15.0 <commit> `
  --add-component extensions extensions.zip <url> 1.0.0 <commit>
# Các lần sau: chỉ liệt kê gói thực sự đổi
python packaging/build_catalog.py --published p4-catalog.json --out payload.json `
  --component app app.zip <url> 1.15.1 <commit>
```

Đăng lên GitHub bằng `publish_release.ps1 -Version <v> -Series p4 -CatalogTag updates-beta-p4` (tag asset `v<v>-p4`; URL trong catalog phải trỏ đúng tag này). Không đăng gói tách vào `updates-*-p3`.

`runtime` chỉ phát hành lại khi nội dung đổi; `RUNTIME_VERSION` và `EXTENSIONS_VERSION` nằm trong `packaging/stage_runtime.py` (phiên bản chính của runtime là ABI). `build_catalog.py` từ chối gói có inventory trùng bản đã đăng (`component_content_unchanged`), nên không thể vô tình đăng lại runtime 48 MB. Resource phiên bản của `KidTE.exe` dùng `RUNTIME_VERSION` (không dùng số phát hành) và build đặt `SOURCE_DATE_EPOCH` + `PYTHONHASHSEED=0`: hai lần build liên tiếp cho cây `runtime` giống nhau từng bit (đã kiểm với số phát hành khác nhau). Dù vậy vẫn nên dùng `-RuntimeZip` cho bộ cài để hash luôn khớp đúng catalog đã đăng, kể cả khi toolchain/thư viện đổi. Ranh giới sở hữu file nằm ở `stage_runtime.component_for`.

## Dọn dẹp cục bộ (garbage collection)

Giống Chrome/VS Code, sau khi một generation được xác nhận khỏe (readiness handshake thành công và journal `committed`), launcher gọi `ComponentStore.collect_garbage()` để xóa mọi `App\packages\<id>\<version>` không thuộc generation đang chạy hoặc `previous.json`: giữ đúng bản hiện tại và một bản để quay lại.

- Không bao giờ chạy khi có `pending.json` (cập nhật hoặc rollback đang chờ), journal ở `prepared/probing`, không có generation active (bản baseline), hoặc metadata hỏng/không đọc được. Sau rollback tự động launcher cũng không dọn; gói lỗi chỉ bị xóa ở lần khởi động khỏe kế tiếp.
- Chống mất điện: thư mục phiên bản được đổi tên nguyên tử vào `.staging\.trash-<uuid>` rồi mới xóa; mapping không bao giờ trỏ vào gói xóa dở, và lần sau mọi `.trash-*` còn sót được dọn.
- Gói mới tải về nhưng chưa vào `pending.json` được bảo vệ bằng thời gian ân hạn (mặc định 1 giờ, biến `KIDTE_GC_GRACE_SECONDS`, tính theo thời điểm tạo thư mục); thất bại nếu cài song song là `prepare` báo "package is not staged" và người dùng thử lại, không hỏng dữ liệu.
- Dọn `.staging` quá 1 ngày (thư mục dang dở, file ZIP tải cũ trong `.staging\downloads`); tải mới đang tiếp tục không bị đụng.
- Chạy dưới khóa `.component-store.lock`; thất bại chỉ ghi `launcher-startup.log`, không chặn khởi động.

## Chứng minh cục bộ

`python packaging/smoke_split_update.py --dist dist/KidTE --work <thư mục tạm> --port 48997` đóng gói ba gói, ký danh mục bằng KHÓA THỬ NGHIỆM sinh tại chỗ (không dùng khóa phát hành) và chạy `KidTELauncher.exe` + `KidTE.exe` thật trên thư mục cài tạm: (a) cập nhật chỉ `app` tải đúng ZIP app; (b) generation lỗi tự quay lại; (c) GC chỉ còn bản hiện tại + một bản trước; (d) chuyển từ `core` nguyên khối sang tách và quay lại. Chỉ dừng các tiến trình do chính script mở.

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
python packaging/update_release.py package <cây-media-tools> --component media-tools --out media-tools.zip
python packaging/update_release.py sign catalog-payload.json `
  --private-key '<ngoài checkout>/release-key.dpapi' --out kidte-catalog-v2.json
```

`keygen` từ chối ghi đè khóa có sẵn. Public key mới phải được ghim và build lại app trước khi dùng catalog tương ứng. Packer lõi kiểm policy CPU, loại launcher ổn định và manifest installer, băm từng file trong lúc ghi và kiểm ZIP trước khi replace output. Công cụ GPU P1 giữ allowlist DLL và license riêng.

Mỗi package record ghi ID, kind, SemVer, URL asset cố định, dung lượng nén/giải nén, hash archive/file, dependency ranges, entrypoint, source commit, API và compatibility. Nâng `sequence` cho mỗi catalog phát hành trên cả hai kênh; không tái sử dụng số cũ. `issued_at`/`expires_at` là UTC; app từ chối catalog hết hạn hoặc thấp hơn sequence đã xác minh. Rollback bộ đã cài không giảm sequence này.

## Bố cục GitHub Releases

Tạo release theo tag phiên bản, tải core ZIP, GPU ZIP cần thiết, installer và checksums đã nghiệm thu. Catalog có thể tham chiếu lại GPU ZIP ở tag cũ để bản vá lõi không bắt tải lại GPU. Mỗi asset phải dưới 2 GiB. [Giới hạn GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).

Catalog P3 nằm ở asset `kidte-catalog-v2.json` của tag `updates-stable-p3` hoặc `updates-beta-p3`; feed P2 cũ được giữ riêng. Catalog cần trỏ tới asset đã upload đầy đủ trước khi công bố. Mỗi lần cập nhật asset catalog phải ký payload mới với sequence lớn hơn; đồng thời cập nhật thời hạn. ETag và thời gian chờ sau lỗi giảm request thừa; app chỉ kiểm khi người dùng yêu cầu. Nguồn package là asset cụ thể, không phải ZIP source do GitHub tự tạo. [API Releases công khai](https://docs.github.com/en/rest/releases/releases).

Phát hành installer chính thức: build core và các cây optional; đóng ZIP, đo hash/file list, ký catalog; chép catalog đã ký thành `dist/KidTE/bootstrap-request.json`, sinh lại release-manifest rồi nén installer. Core ZIP loại launcher, release-manifest và bootstrap request để tránh vòng hash. Khi chưa có managed core, updater cũng có thể kiểm inventory lõi ở root và dùng bản cài khớp, không tải lại core ZIP. Nếu khác byte thì trình bày đúng dung lượng cần tải.

Installer và core ZIP mới phải được build từ commit nguồn chốt. Catalog ký đúng SHA256 đo từ artifact thực; việc ký không thay nghiệm thu máy Windows sạch, cài/nâng cấp, WebView và phiên LIVE.

Nếu bản vá chỉ sửa installer, có thể phát hành riêng bộ cài với nhãn mới (ví dụ `p3-r1`) và giữ nguyên core ZIP/catalog đã phát hành. Không ghi đè asset cũ. Ghi rõ commit của installer/manifest, commit và URL/hash core được dùng lại, cùng lý do không rebuild core; xác minh diff không đổi mã hoặc nội dung core. Bộ cài sửa vẫn phải được tải từ GitHub và nghiệm thu trên snapshot Windows chưa có app/runtime phụ thuộc.

## Dữ liệu và phục hồi

`App/packages/<id>/<version>` chứa gói (chỉ bản đang chạy và một bản trước được giữ); `.staging` chứa tải dở; `active.json`, `pending.json`, `previous.json` và `update-journal.json` thuộc metadata chương trình. Điểm chuyển phiên bản là replace atomic của mapping. Khóa hệ điều hành ngăn hai updater chuyển metadata đồng thời. Nhật ký không hoàn tất sẽ đưa về bộ trước; nhật ký cũ không xóa pending của lần cập nhật tiếp theo.

`DuLieu` nằm ngoài package. Launcher không mở hoặc migrate DB trước khi khởi động lõi. Cơ chế rollback này dành cho cập nhật cùng schema đã ghim, không phải rollback dữ liệu. Bản lõi có schema khác phải có quy trình migration và phục hồi được nghiệm thu riêng trước khi catalog được cho phép.
