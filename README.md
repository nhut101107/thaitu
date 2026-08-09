# NFToken Pro Bot + Telegram Mini App

Ứng dụng giữ nguyên bot Telegram trong `code_goc.py` và bổ sung Mini App ở thư mục `miniapp/`. API Flask dùng chung SQLite với bot, xác thực chữ ký `initData` Telegram trước mọi API riêng tư và không nhận Telegram ID từ frontend.

## Ánh xạ dữ liệu thật

- `users`: hồ sơ, số dư, lượt Cookie VIP và gói thành viên.
- `store`: sản phẩm/gói đang bán. Sản phẩm nổi bật là các bản ghi có `featured=1`; nếu chưa đánh dấu, trang chủ lấy các sản phẩm mới nhất.
- `purchase_history`: đơn hàng. Mini App chỉ truy vấn đơn theo Telegram ID đã xác thực.
- `transactions`: lịch sử yêu cầu nạp tiền.
- `premium_cookies`: dùng để hiển thị tồn kho tổng quan; dữ liệu cookie không bao giờ được trả về frontend.
- `miniapp_cart`, `miniapp_checkouts`: giỏ hàng và khóa idempotency mới, được tạo tự động bởi migration không phá schema cũ.

Bot hiện không có TOTP, referral hoặc quy trình yêu cầu bảo hành. Mini App không hiển thị nút giả cho các chức năng này; đơn hàng chỉ hiển thị thời hạn bảo hành nếu admin cấu hình `warranty_days`.

Các chức năng người dùng của bot hiện chạy trực tiếp trong Mini App: tạo NFToken theo gói, rút Cookie VIP, nhận Cookie miễn phí, nhập mã Netflix TV, đổi giftcode, tạo yêu cầu nạp tiền và gửi hỗ trợ. Mini App không đóng cuộc trò chuyện Telegram khi thao tác. Các chức năng quản trị vẫn nằm trong `/admin` để không mở quyền quản trị nhạy cảm qua giao diện web công khai.

## Chạy local

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN='token-tu-BotFather'
export TELEGRAM_MINIAPP_URL='https://domain-https-cua-ban'
python miniapp_server.py
```

Mở terminal thứ hai với cùng biến môi trường:

```bash
python code_goc.py
```

Mini App production bắt buộc HTTPS. Reverse proxy domain HTTPS đến `127.0.0.1:8080`, đặt URL đó vào `TELEGRAM_MINIAPP_URL`, sau đó cấu hình cùng URL trong BotFather (`/newapp` hoặc `/myapps`). Nút Mini App chỉ xuất hiện khi biến này bắt đầu bằng `https://`; các nút bot cũ vẫn được giữ nguyên.

Để nút nạp tiền tạo QR VietQR, cấu hình thêm `VIETQR_BANK_BIN`, `VIETQR_ACCOUNT_NUMBER` và `VIETQR_ACCOUNT_NAME`. Nếu chưa cấu hình, yêu cầu nạp vẫn được tạo và gửi Admin nhưng giao diện chỉ hiển thị nội dung chuyển khoản.

## Cấu hình sản phẩm mở rộng

Migration tự thêm các cột tương thích ngược vào `store`: `description`, `category`, `image_url`, `featured`, `warranty_days`, `active`, `purchases`. Admin cũ vẫn thêm được gói như trước. Có thể cập nhật metadata bằng SQLite/admin tooling hiện có; không có dữ liệu sản phẩm mock trong production.

## Bảo mật checkout

Backend đọc lại giá từ SQLite, bắt đầu `BEGIN IMMEDIATE`, kiểm tra số dư, trừ tiền, cộng lượt, tạo lịch sử mua và xóa giỏ trong cùng transaction. Cặp `(user_id, idempotency_key)` là duy nhất để chống bấm mua nhiều lần. API chi tiết đơn luôn lọc đồng thời theo `id` và `user_id`.
