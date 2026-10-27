# Lấy API key AI miễn phí và điền vào .env

Giới hạn và model của gói miễn phí thay đổi thường xuyên. Xem số liệu hiện hành ngay trong
trang quản lý của từng nhà cung cấp, đừng tin các con số trong tài liệu bên thứ ba.

## Cách 1 — Google Gemini (khuyên dùng để bắt đầu)
1. Vào https://aistudio.google.com/apikey, đăng nhập tài khoản Google.
2. Bấm **Create API key**. Không cần nhập thẻ thanh toán.
3. Điền vào `.env`:
   ```
   AI_PROVIDER=gemini
   GEMINI_API_KEY=<key vừa tạo>
   GEMINI_MODEL=gemini-2.5-flash
   ```
   Nếu model báo không tồn tại hoặc không còn miễn phí, chọn model khác ở AI Studio rồi sửa `GEMINI_MODEL`.
4. Lưu ý: không bật Cloud Billing cho project của key, nếu không sẽ bị tính phí. Gói miễn phí có thể
   dùng dữ liệu gửi lên để cải thiện sản phẩm của Google. Dữ liệu gửi đi chỉ gồm số liệu thị trường
   và tiêu đề tin, không có API key hay số dư.

## Cách 2 — Groq (nhanh, miễn phí, giới hạn theo từng model)
1. Vào https://console.groq.com, đăng ký, vào **API Keys** rồi **Create API Key**.
2. Điền vào `.env`:
   ```
   AI_PROVIDER=groq
   GROQ_API_KEY=<key>
   GROQ_MODEL=llama-3.3-70b-versatile
   ```
3. Xem hạn mức tại https://console.groq.com/settings/limits.

## Anthropic và OpenAI
API của hai bên này tính phí theo lượng dùng, không có gói miễn phí lâu dài. Chỉ dùng khi anh/chị
chủ động nạp tiền. Điền `ANTHROPIC_API_KEY` hoặc `OPENAI_API_KEY` và đặt `AI_PROVIDER` tương ứng.

## Kiểm tra
Sau khi sửa `.env`, khởi động lại backend, rồi gọi `GET /api/ai/status`. Mục `configured` của
nhà cung cấp đã chọn phải là `true`. Gọi `POST /api/analysis/BTCUSDT/run`: nếu có lỗi AI, trường
`ai_error` trong báo cáo sẽ cho biết lý do, còn chỉ báo kỹ thuật vẫn hiển thị bình thường.

## Giảm số lần gọi
Mặc định chỉ phân tích lại khi có nến mới đóng hoặc giá lệch từ 0,5%. Để tiết kiệm hạn mức miễn phí,
hạ `AI_DAILY_CALL_LIMIT` (ví dụ 100) và theo dõi ít coin hơn. Hết hạn mức, hệ thống vẫn chạy bình thường,
chỉ không có phần diễn giải của AI.

Không bao giờ dán key vào chat hoặc commit file `.env` (đã nằm trong `.gitignore`).
