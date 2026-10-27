# Runbook: chuyển sang LIVE (tiền thật)

Đạt mọi kiểm tra dưới đây chỉ là điều kiện cần. Không có gì ở đây bảo đảm lợi nhuận. Chỉ dùng số vốn
bạn chấp nhận mất hoàn toàn.

## 0. Thứ tự bắt buộc
1. `PAPER_TRADING` đủ 30 ngày / 30 lệnh, chạy backtest (trang Backtest), đạt ngưỡng ở `backend/app/validation.py`.
2. `BINANCE_TESTNET`: thử trọn luồng (mua, OCO, bán, kill switch) với key testnet.
3. Mới sang LIVE với vốn rất nhỏ.

## 1. API key Binance (mainnet)
- Chỉ bật **Enable Spot & Margin Trading**. TẮT rút tiền, margin, futures, chuyển nội bộ.
- **Giới hạn IP** = IP VPS. Preflight chặn nếu key cho phép rút tiền hoặc không giới hạn IP.
- Key chỉ nằm trong `.env` trên VPS (quyền `chmod 600`), không commit.

## 2. Cấu hình `.env`
```
TRADING_MODE=LIVE
BINANCE_ENV=mainnet
LIVE_TRADING_ACK=I_UNDERSTAND_REAL_MONEY_RISK
LIVE_MAX_ORDER_USDT=50          # mỗi lệnh mua
LIVE_MAX_DAILY_LOSS_USDT=20     # chạm mức này trong ngày (UTC) thì tự tắt giao dịch
LIVE_MAX_OPEN_POSITIONS=2
TELEGRAM_BOT_TOKEN=...          # nên có; thiếu thì chỉ có log + audit
TELEGRAM_CHAT_ID=...
```
Giới hạn `<= 0` nghĩa là chặn hẳn. Lỗ ngày = vốn đầu ngày trừ vốn hiện tại; nạp/rút tiền làm lệch con số,
và nếu backend khởi động giữa ngày thì mốc là điểm vốn đầu tiên sau khi khởi động.

## 3. Trước khi bật
1. Trang **Cài đặt, Giao dịch thật, Chạy preflight**. Mọi dòng "CHẶN" phải thành "ĐẠT". Backend cũng chạy lại
   preflight khi bạn bật công tắc LIVE và từ chối nếu chưa đạt.
2. Đợi vài phút để vòng đối soát chạy ít nhất một lần (preflight yêu cầu).
3. Bật công tắc (gõ `ENABLE TRADING`). Mỗi đề xuất vẫn cần bạn xác nhận riêng.

## 4. Vận hành
| Sự kiện | App làm gì | Bạn làm gì |
|---|---|---|
| Lỗ ngày chạm giới hạn | Tự tắt công tắc, cảnh báo | Xem lại; công tắc không tự bật lại |
| N lỗi sàn liên tiếp | Tự tắt công tắc | Xem trang Lệnh/audit |
| Lệnh không rõ kết quả (`UNKNOWN_NEEDS_REVIEW`) | Tắt công tắc, cảnh báo | **Kiểm tra trực tiếp trên Binance** trước khi làm gì khác |
| Lệnh bảo vệ (OCO) biến mất khi vị thế còn mở | Đánh dấu chưa bảo vệ, cảnh báo | Đặt lại ở trang Portfolio hoặc bán |
| Lệnh lạ trên sàn / số dư lệch DB | Cảnh báo một lần | Tự tay kiểm tra |
| Vị thế đóng (stop/TP khớp) | Cảnh báo | Không |

**Dừng khẩn cấp** (Cài đặt): *Dừng* chỉ tắt giao dịch, không đụng tới stop đang chạy. *Dừng + huỷ mọi lệnh*
huỷ cả OCO nên các vị thế sẽ KHÔNG còn stop trên sàn. Chỉ dùng khi bạn sẽ tự xử lý vị thế ngay.

Nếu backend chết hoặc không truy cập được giao diện: đăng nhập Binance và huỷ/đặt lệnh trực tiếp. OCO đã
đặt vẫn chạy trên sàn dù app tắt.

## 5. Triển khai VPS
- `docker compose up -d --build` (cần `DB_PASSWORD`, `WEB_PORT`, `ADMIN_PASSWORD`, `SESSION_SECRET` >= 32 ký tự).
- Cổng chỉ bind `127.0.0.1`; đặt `deploy/nginx.coin.conf` làm reverse proxy HTTPS, nên kèm giới hạn IP/VPN.
- Sao lưu: cron `deploy/backup.sh` hằng ngày.
- Đồng hồ VPS phải chuẩn (chrony/systemd-timesyncd); preflight chặn khi lệch quá 1 giây.
- Sau mỗi lần sửa code: chạy lại toàn bộ test (`pytest`) và quay lại testnet trước khi đưa lên LIVE.

## 6. Quay về an toàn
Đổi `TRADING_MODE=PAPER_TRADING` (hoặc `ANALYSIS_ONLY`) trong `.env`, rồi `docker compose up -d coin-api`.

## 7. Tự động mua/thoát
- **PAPER_TRADING** đã tự mua và tự thoát trên sổ giả lập khi có tín hiệu, không cần cấu hình.
- **BINANCE_TESTNET / LIVE**: công tắc riêng ở Cài đặt (gõ `ENABLE AUTO`), mặc định TẮT. Chỉ bật được khi công tắc giao dịch
  đã bật; ở LIVE còn phải qua preflight. Tắt giao dịch, dừng khẩn cấp hay chạm lỗ ngày là tự động cũng tắt, bật lại có chủ ý.
- Tự động chỉ **mua mới** khi có `BUY_SETUP` và **thoát toàn bộ** (SELL_EXIT: 1h+4h cùng DOWN, hoặc chạm stop/TP). **Giảm nửa** vẫn cần bạn xác nhận.
- Mỗi lệnh tự động đi qua đúng `executor.confirm` như khi bạn bấm, nên giữ nguyên mọi kiểm tra (30 ngày paper + backtest,
  lỗ ngày, giới hạn vốn, dữ liệu tươi, rủi ro, OCO xác minh). Khối lượng bị cắt xuống `AUTO_MAX_ORDER_USDT` nếu vượt.
- Giới hạn thêm: `AUTO_MAX_BUYS_PER_DAY`, nghỉ `AUTO_COOLDOWN_S` sau mỗi lần mua thành công, thử lại không dồn dập (`AUTO_RETRY_S`).
- Mỗi hành động tự động (`auto_buy`, `auto_exit`, `auto_failed`) ghi audit và gửi Telegram.
- Hãy chạy tự động trên **testnet** vài ngày và đối chiếu với Binance testnet trước khi nghĩ tới LIVE.
