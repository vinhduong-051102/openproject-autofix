"""Checklist "sửa theo loại lỗi" + các biến thể tester hay test thêm khi verify.

Rút từ các comment Reopened của tester trên dự án SOAR (quét 45 ngày tới 26/11/2026, 73 lần reopen), ví dụ:
- SOAR-3086: kiểm quyền trước validate nhưng body sai kiểu / mảng vẫn 400 trước 403; sót API cùng nhóm (PUT /server-time).
- Nhóm quyền ADMIN FULL: ẩn nút Xoá trên UI nhưng API PUT vẫn 200 (chỉ chặn ở giao diện, sót thao tác Sửa).
- Lịch sử mức độ nguy hiểm: tiếng Việt đúng, tiếng Anh vẫn sai.
- QR Google Authenticator: user_id không tồn tại vẫn 200; nhãn gán cứng theo người gọi thay vì user được yêu cầu.
- Lịch sử người dùng: DB null, FE gửi "" → coi là có thay đổi, sinh log rác.
- Báo cáo / Bảng điều khiển: bản ghi thiếu trường (không có trạng thái) bị đếm thiếu ở một màn hình.
- Lọc SLA: chưa cắt khoảng trắng đầu / cuối; ô nhập lệch kích thước so với control bên cạnh.
- Cấu hình kết nối: báo trùng rồi đổi tuỳ chọn (Không bắt buộc) vẫn không lưu được (chuyển trạng thái).
- Giao diện vỡ: sửa bên trái nhưng bên phải / tiêu đề vẫn lỗi; nhiều bản ghi không cuộn được.

Dùng chung cho prompt sửa (Claude), prompt test lại (Gemini đọc code) và QA trên server (Gemini / Claude).
Text giữ nguyên dấu ngoặc nhọn — template dùng .format phải chèn qua esc().
"""

FIX_SCOPE_RULES = """SỬA THEO LOẠI LỖI, KHÔNG THEO VÍ DỤ (tester verify bằng nhiều biến thể hơn ví dụ trong ticket — sửa vừa
khít ví dụ là nguyên nhân reopen phổ biến nhất):
- Xác định LOẠI lỗi (vd "kiểm quyền sau validate", "chỉ chặn ở giao diện", "thiếu bản tiếng Anh", "null khác chuỗi
  rỗng") rồi sửa ở TẦNG GỐC để mọi biến thể đều đúng. Ví dụ: kiểm quyền phải chạy TRƯỚC khi Spring đọc / chuyển body
  (@RequestBody bị parse trước khi vào hàm controller → body sai kiểu, mảng, JSON hỏng sẽ ra 400 trước khi tới dòng
  kiểm quyền): nhận body thô (JsonNode / String) rồi kiểm quyền trước khi chuyển sang DTO, hoặc kiểm ở interceptor /
  annotation chạy trước bước đọc body.
- Rà CÙNG LOẠI lỗi ở mọi endpoint / màn hình cùng nhóm (cùng controller, cùng màn hình, cùng chức năng ở luồng khác)
  và sửa luôn — tester test cả nhóm ("như các API khác").
- Đi hết các biến thể trong CHECKLIST BIẾN THỂ dưới đây thuộc loại lỗi của ticket."""

VARIANT_CHECKLIST = """CHECKLIST BIẾN THỂ (chọn các mục thuộc loại lỗi của ticket):
1. Phân quyền: chặn ở BE (API), không chỉ ẩn nút; đủ mọi thao tác (xem / thêm / sửa / xoá / xuất); tài khoản không quyền
   nhận 403 với MỌI body: hợp lệ, sai giá trị, {}, null, sai kiểu ("abc" thay số / boolean), mảng [], JSON hỏng; cặp đối
   chứng tài khoản có quyền vẫn chạy đúng (2xx / 400 validate đúng mã).
2. Dữ liệu vào: {}, null, "" (và null khác "" khi so sánh thay đổi / ghi log), sai kiểu, mảng thay object, khoảng trắng
   đầu / cuối, độ dài biên (max, max+1), ký tự đặc biệt / Unicode; id không tồn tại → 404 (không 200 kèm dữ liệu giả),
   id thuộc người / tenancy khác.
3. Ngôn ngữ: cả tiếng Việt và tiếng Anh (Accept-Language vi / en) cho thông báo, lịch sử / nhật ký, email, bell, nhãn.
4. Dữ liệu thật: bản ghi thiếu trường (null), nhiều bản ghi (cuộn, phân trang), sửa nhiều trường cùng lúc (định dạng
   log), số liệu phải khớp giữa các màn hình cùng hiển thị (báo cáo / bảng điều khiển / danh sách).
5. Luồng và trạng thái: mọi đường vào cùng chức năng (vd đổi mật khẩu sau đăng nhập lẫn quên mật khẩu; tạo lẫn sửa),
   chuyển qua lại tab, đổi tuỳ chọn sau khi đã báo lỗi rồi lưu lại, mở lại sau khi lưu.
6. Giao diện: mọi vùng của màn hình ticket nêu (trái / phải / tiêu đề / modal), kích thước đồng đều với control bên
   cạnh, sáng / tối."""

VARIANT_SECTION = "Biến thể đã đối chiếu"


def esc(text):
    """Chèn vào template dùng str.format: nhân đôi ngoặc nhọn."""
    return text.replace("{", "{{").replace("}", "}}")
