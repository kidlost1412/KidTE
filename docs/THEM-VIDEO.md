# Thêm video phát lại và lịch ghim

Mỗi video là một thư mục dữ liệu `data/videos/<video_id>/`:

    timeline.json      lịch ghim + ngữ cảnh đoạn (host đang nói gì)
    fingerprint.npz    vân tay âm thanh — để nhận ra video đang phát
    transcript.json    lời thoại (Studio dùng để đối chiếu lịch ghim)

Dịch vụ **Đồng bộ video** so âm thanh đang phát với mọi vân tay trong thư viện,
tự biết đang phát video nào, ở giây thứ mấy; tiện ích Chrome tải đúng lịch của
video đó và ghim theo. Thêm video không cần sửa mã, không cần tải lại tiện ích.

## Cách làm trong app (khuyên dùng)

1. **Video & lịch ghim → Tải video lên**. Tải lên có thể tạm dừng/tiếp tục; video
   không rời khỏi máy.
2. App tự **chép lời** (faster-whisper), dựng **vân tay** và **đề xuất mốc ghim**
   từ lời host nói (kèm câu/đoạn transcript và độ chắc chắn).
3. Mở **Duyệt AI** trong Studio: Enter = đúng, X = sai, N = mốc chưa duyệt; mỗi
   N đi tới mốc chưa duyệt tiếp theo, không đổi trạng thái mốc hiện tại. Mỗi
   lần chuyển mốc tự phát từ 3 giây trước để nghe câu dẫn. Tab **Kiểm tra** chỉ ra
   chỗ host nhắc sản phẩm khác sản phẩm đang ghim.
4. **Phát hành** → video vào thư viện, dùng được ngay khi LIVE.
5. Sau này chỉnh tiếp trong Studio → **Áp dụng cho LIVE**. Giỏ hàng đổi số thì bấm
   **Cập nhật số giỏ** (giờ ghim và sản phẩm giữ nguyên).

Phím tắt Studio: `?`.

## Cách làm bằng dòng lệnh (người phát triển)

    backend\.venv\Scripts\python.exe backend\tools\add_video.py --id live3 ^
        --video "D:\...\live3.mp4" --transcript "D:\...\transcript.json" --title "Live 3"

Tạo vân tay + lịch nháp; rồi mở Studio để rà. Chỉ dựng lại vân tay (video bị cắt
lại / đổi nhạc nền): thêm `--only-fingerprint` — `timeline.json` được giữ nguyên.

CLI ghi vân tay trước bước kiểm timeline đã tồn tại. `--force` bảo vệ việc ghi
timeline, không bảo vệ vân tay; kiểm video đích trước khi chạy lại lệnh. Đối chiếu
`backend/tools/add_video.py` và [chương04](kien-thuc/04-VIDEO-AM-THANH.md).

## Cấu trúc timeline.json

```json
{
  "schema_version": 1,
  "video_id": "live3",            // trùng tên thư mục
  "title": "Live 3",
  "cycle_ms": 1140773,            // độ dài THẬT của video
  "repin_every_ms": 20000,        // chu kỳ bỏ ghim/ghim lại
  "products": {
    "sku_1730421812382697818": {
      "product_id": "1730421812382697818",
      "bag_index": 1,             // số giỏ lúc làm lịch
      "short_name": "Đạm Cá 5L",
      "title": "Đạm Cá Hữu Cơ Amino (Can 5 Lít)",
      "include_all": ["dam ca huu co amino"],
      "exclude_any": []
    }
  },
  "events": [
    { "at_ms": 0, "product": "sku_1730421812382697818" },
    { "at_ms": 302000, "product": null },                       // null = bỏ ghim
    { "at_ms": 510000, "product": "sku_…", "alternate_with": "sku_…",
      "alternate_every_ms": 60000 }                             // luân phiên 2 SKU
  ],
  "script_contexts": [
    { "at_ms": 0, "topic": "Đạm cá cho cây con", "facts": ["Đoạn này host hướng dẫn phun qua lá."] }
  ]
}
```

Luật bắt buộc (tiện ích và backend đều kiểm; sai thì **không nạp**, giữ lịch đang dùng):
`events[0].at_ms = 0`; `at_ms` tăng dần và nhỏ hơn `cycle_ms`; mọi `product` /
`alternate_with` có trong `products`; `alternate_with` kèm `alternate_every_ms`;
`include_all` không rỗng; `video_id` trùng tên thư mục.

`facts` là căn cứ cho AI khi trả lời trong đoạn đó — không ghi giá, liều hay cam
kết hiệu quả vào đây (những thứ đó chỉ lấy từ hồ sơ sản phẩm đã duyệt).

Nhận ra video mà chưa có `timeline.json` thì tiện ích giữ lịch đang dùng và cảnh
báo trên widget, không ghim bừa.
