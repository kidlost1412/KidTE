# AGENTS.md — hướng dẫn cho Codex/ChatGPT

## Mục đích
Kid TE là trợ lý chạy trên máy cho việc bán hàng LIVE TikTok/Shopee. Windows, Python 3.11 x64; backend phục vụ tại 127.0.0.1:8793. Trả lời chủ dự án bằng tiếng Việt, ngắn gọn.

## Cấu trúc
- `backend/src`: mã backend (`app_entry.py` là điểm vào duy nhất, `server.py`, `config.py`).
- `backend/tests`: pytest và `tests/js/*.test.mjs`. `backend/tools`: công cụ (có `run_tests.py`).
- `extension` (TikTok), `extension-shopee`, `plugins`, `ui`, `packaging`, `docs`.

## Kiểm thử
- Chạy: `.venv\Scripts\python.exe backend\tools\run_tests.py --suite all` (suite: all|python|web|extension; web/extension cần Node).
- Tên các suite extension được khoá trong `run_tests.py`; không xoá/đổi tên để qua test.
- Test tự cô lập (conftest đặt `KID_TE_ROOT` tạm). Không bao giờ trỏ test vào dữ liệu thật của người dùng.

## Chạy từ mã nguồn
`backend\src\app_entry.py --port <khác 8793> --no-ui --safe-mode`, với `$env:KID_TE_ROOT` trỏ tới bản sao dùng-một-lần. Không dùng dữ liệu hay cổng của app đã cài.

## Quy tắc
- Làm trong nhánh/worktree, không commit thẳng vào main; test xanh trước khi gộp.
- Không sửa/skip/xoá test cho khớp code sai.
- Repo công khai: không commit `.env`, khoá API, `*.dpapi`, khoá ký, `*.sqlite3`, log hay dữ liệu khách.
- Không push, đăng, gắn tag. Chủ dự án tự phát hành qua `packaging/publish_release.ps1`.
- Không build lại số phiên bản đã phát hành.
- `packaging/build.ps1` phải chạy qua `Start-Process` có chuyển hướng stdout/stderr.

## Quy tắc sản phẩm (không đổi khi chưa có chủ dự án đồng ý)
- AI không được bịa giá, quà tặng, khuyến mãi (giá/quà là dữ kiện cứng, code kiểm tra). Ngoài ra đừng siết AI quá mức.
- Khách công kích: không nhận lỗi, mời khách nhắn TikTok Shop.
- Extension TikTok tự ghim/bỏ ghim mỗi 20 giây là chủ ý, giữ nguyên.

## Khi review
- Báo kết quả lệnh thật kèm exit code; chưa chạy thì ghi "chưa kiểm".
- Ưu tiên thay đổi nhỏ, đúng chỗ.
