# Kid TE 1.15.8

Trang Cập nhật có nút **Cập nhật toàn bộ**: gom bản mới của các gói đã cài và các tính năng còn thiếu (công cụ video, CAPTCHA, đồng bộ video, chép lời trên máy). Hiện danh sách cùng tổng tải thực tế sau khi giải quyết phụ thuộc; chỉ chuẩn bị một bộ cập nhật và khởi động lại một lần khi hoàn tất.

Máy đã có ứng dụng mới nhất vẫn thấy nút này nếu còn thiếu tính năng. Gói NVIDIA được tự chọn khi nhận diện card và driver phù hợp; gói NVIDIA đã cài vẫn được cập nhật cùng các gói khác. Nút **Chỉ cập nhật gói đã cài** giữ lựa chọn cập nhật nhẹ. Model chép lời được tải riêng trong trang Chép lời.

Sửa hộp xác nhận trên trang Cập nhật để hiển thị đúng nội dung và tên nút theo API giao diện hiện tại. Kiểm chữ ký, hash, phụ thuộc và cơ chế khôi phục giữ nguyên; không tự khởi động lại giữa từng gói.

Máy cài mới bằng bộ cài 1.15.8 tự kiểm tra phần cứng và tải bộ tính năng ban đầu trong lần mở đầu tiên. Không có NVIDIA phù hợp thì dùng CPU. Trạng thái được lưu để không tự cài lại tính năng đã gỡ ở các lần mở sau. Chỉ chuẩn bị gói và yêu cầu khởi động lại sau khi toàn bộ lượt tải hoàn tất, không tự ngắt LIVE. Cài đè/nâng cấp máy đang có dữ liệu không bật thiết lập tự động này.

Phát hành app 1.15.8 qua kênh Thử nghiệm, dùng lại runtime 1.0.0 và extensions 1.0.1.

Nâng giới hạn video từ 20 lên 100 GB/file (100 × 1024³ byte), kiểm tra ở cả giao diện và backend. Giữ tải theo từng phần 8 MB và khả năng tiếp tục; không nạp toàn bộ video lớn vào RAM. Ổ dữ liệu cần đủ chỗ lưu bản video được tải lên cùng các file phân tích.
