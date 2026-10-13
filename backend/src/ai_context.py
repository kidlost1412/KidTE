"""Ngữ cảnh chung cho MỌI lần gọi AI (DeepSeek, Gemini...).

Mỗi lời gọi = APP_BRIEF (cố định, đứng đầu để nhà cung cấp lưu đệm prefix)
+ một khối nhiệm vụ riêng (task_block): đang ở chức năng nào, người dùng muốn gì,
đầu vào là gì, phải trả về gì. Giữ APP_BRIEF ngắn và KHÔNG chèn dữ liệu biến
đổi vào đây, nếu không sẽ mất lợi ích cache.
"""
from __future__ import annotations

APP_BRIEF = """\
[KID TE — BỐI CẢNH CHUNG]
Kid TE là trợ lý bán hàng livestream chạy trên máy Windows của một shop nông nghiệp
(phân bón, thuốc bảo vệ thực vật, kích rễ...) bán trên TikTok LIVE và Shopee LIVE.
Người dùng là chủ shop hoặc người trực live; khách là nông dân hỏi qua bình luận.
Dữ liệu app có: hồ sơ sản phẩm đã duyệt (thành phần, công dụng, liều, lưu ý), giỏ
hàng đang live (tên, giá, quy cách), thông tin shop, trí nhớ từng khách, quy tắc
và câu mẫu shop đã dạy, phong cách của người đang trực.
Quy tắc vàng: giá, quà tặng, khuyến mãi, cam kết giao hàng CHỈ lấy từ dữ liệu shop,
tuyệt đối không bịa. Kiến thức kỹ thuật nông nghiệp được dùng hiểu biết chung khi
hồ sơ thiếu, nói vừa phải và nhắc xem nhãn khi cần. Không tiết lộ dữ liệu của khách
này cho khách khác. Viết tiếng Việt tự nhiên.
"""


def task_block(task: str, goal: str, *, inputs: str = "", output: str = "") -> str:
    """Khối nhiệm vụ ngắn đặt SAU APP_BRIEF."""
    lines = [f"[NHIỆM VỤ: {task}]", f"Mục đích: {goal}"]
    if inputs:
        lines.append(f"Đầu vào: {inputs}")
    if output:
        lines.append(f"Trả về: {output}")
    return "\n".join(lines)


def system_with_brief(*parts: str) -> str:
    """APP_BRIEF luôn đứng đầu, sau đó các phần riêng của chỗ gọi."""
    return "\n\n".join([APP_BRIEF.rstrip(), *[p.strip() for p in parts if p and p.strip()]])
