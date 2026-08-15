# NFToken Pro Bot + Telegram Mini App

Ứng dụng giữ nguyên bot Telegram trong `code_goc.py` và bổ sung Mini App ở thư mục `miniapp/`. API Flask dùng chung SQLite với bot, xác thực chữ ký `initData` Telegram trước mọi API riêng tư và không nhận Telegram ID từ frontend.

## Ánh xạ dữ liệu thật

- `users`: hồ sơ, số dư, lượt Cookie VIP và gói thành viên.
- `store`: sản phẩm/gói đang bán. Sản phẩm nổi bật là các bản ghi có `featured=1`; nếu chưa đánh dấu, trang chủ lấy các sản phẩm mới nhất.
- `purchase_history`: đơn hàng. Mini App chỉ truy vấn đơn theo Telegram ID đã xác thực.
- `transactions`: lịch sử yêu cầu nạp tiền.
- `premium_cookies`: dùng để hiển thị tồn kho tổng quan; dữ liệu cookie không bao giờ được trả về frontend.
- `miniapp_cart`, `miniapp_checkouts`: giỏ hàng và khóa idempotency mới, được tạo tự động bởi migration không phá schema cũ.

Bot chỉ đăng ký `/start` để hỗ trợ Telegram deep-link/referral. Khi `TELEGRAM_GROUP_GATE_ENABLED=1`, người dùng mới phải tham gia `TELEGRAM_REQUIRED_GROUP`, quay lại bot và bấm nút xác nhận; bot gọi `getChatMember` rồi mới cấp nút Web App **Shop MMO**. Chỉ bật công tắc sau khi bot đã được thêm làm Admin của nhóm. Backend cũng kiểm tra cờ xác nhận trong SQLite nên link Web App cũ không thể bỏ qua bước này. Referral, quản lý thiết bị, lịch sử giao hàng và bảo hành được xử lý trực tiếp trong Mini App; người dùng không thể tự sửa hạng hoặc quyền lợi.

Các chức năng người dùng của bot hiện chạy trực tiếp trong Mini App: tạo NFToken theo gói, rút Cookie VIP, nhận Cookie miễn phí, nhập mã Netflix TV, đổi giftcode, tạo yêu cầu nạp tiền và gửi hỗ trợ. Luồng nạp tiền hoạt động hoàn toàn trong app: khách tạo QR, bấm “Tôi đã chuyển tiền”, theo dõi trạng thái; Admin duyệt hoặc từ chối kèm lý do và kết quả tự cập nhật cho khách. Mini App không đóng cuộc trò chuyện Telegram khi thao tác.

Telegram ID trong `TELEGRAM_ADMIN_ID` có thêm Trung tâm quản trị riêng ngay trong Mini App. Admin có thể quản lý sản phẩm/provider, giá, mã giảm phần trăm, Flash Sale, hạng khách hàng, referral, mission, lượt trial, bảo hành, thiết bị/risk, đối soát thanh toán, người dùng, giftcode, hỗ trợ và kho Cookie. Kho có scan nền, cách ly lỗi mạng và cảnh báo sắp hết; không có API đọc ngược Cookie hoặc API key. Mọi API `/api/admin/*` đều xác thực chữ ký Telegram và kiểm tra lại Admin ID ở backend; việc ẩn nút trên frontend không được dùng làm lớp bảo mật.

Mỗi sản phẩm có hai quyền lợi tách biệt: `nftoken_credits` là số lượt tạo link NFToken đã mua và `credits` là số lượt lấy Cookie VIP. Checkout cộng hai loại lượt trong cùng transaction. Khi tạo NFToken, hệ thống ưu tiên dùng lượt đã mua; nếu hết mới dùng hạn mức hằng ngày của gói. Nếu kiểm tra Cookie thất bại, đúng loại lượt vừa dùng sẽ được hoàn lại.

Admin có thể chọn nhiều file hoặc chọn nguyên thư mục Cookie từ Mini App. Backend nhận `.txt`, `.nem`, `.zip`, `.rar`; report `.nem`/text có dòng `🍪 Cookie:` sẽ chỉ lấy Cookie ở dòng đó và bỏ qua các dòng Phone Login/PC Login. File khác tự bỏ qua, archive không giải nén ra filesystem, archive mã hóa/ZIP bomb bị chặn, dữ liệu trùng được gom và kiểm tra song song ở nền. Mặc định mỗi lần nhận tối đa 512MB, 10.000 file và 250.000 Cookie; có thể tăng/giảm bằng `INVENTORY_MAX_UPLOAD_MB`, `INVENTORY_MAX_FILE_MB`, `INVENTORY_MAX_FILES`, `INVENTORY_MAX_ENTRIES` trong `.env`. Chỉ tài khoản `CURRENT_MEMBER` tạo được NFToken mới được lưu; giao diện hiển thị tiến độ live/lỗi/trùng và không chặn người dùng khác.

## Windows Server 2022

Trên VPS Windows, cài Python 3.11+ rồi mở PowerShell tại thư mục repo:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
Copy-Item .env.example .env
notepad .env
powershell -ExecutionPolicy Bypass -File .\start_windows.ps1
```

Điền token, Admin ID, URL Mini App HTTPS, đường dẫn SQLite và giới hạn kho trong `.env`. Tự tạo giá trị ngẫu nhiên riêng cho `MINIAPP_DOWNLOAD_SECRET`; nếu dùng webhook thanh toán, cấu hình thêm `PAYMENT_WEBHOOK_SECRET` và không chia sẻ hai giá trị này. Script tự tạo `.venv`, cài requirements, chạy Mini App, bot và watchdog; log nằm trong thư mục `logs\\`. Để Telegram truy cập được từ Internet, dùng domain HTTPS qua IIS/reverse proxy hoặc Cloudflare Tunnel trỏ vào `MINIAPP_HOST:MINIAPP_PORT`; không dùng `127.0.0.1` làm URL Telegram.

Trong Admin → Quản lý kho Cookie, chọn “Chọn nhiều file” hoặc “Chọn thư mục”. Có thể đưa cả folder chứa hàng nghìn TXT; file không hỗ trợ sẽ bị bỏ qua, Cookie được lọc live ở nền và kết quả tự cập nhật trên app.

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

Mini App production bắt buộc HTTPS. Reverse proxy domain HTTPS đến `127.0.0.1:8080`, đặt URL đó vào `TELEGRAM_MINIAPP_URL`, sau đó cấu hình cùng URL trong BotFather (`/newapp` hoặc `/myapps`). Không đặt Web App làm menu button toàn cục vì sẽ bỏ qua cổng thành viên; nút **Shop MMO** chỉ được bot gửi riêng sau khi xác nhận thành công.

Để nút nạp tiền tạo QR VietQR, cấu hình thêm `VIETQR_BANK_BIN`, `VIETQR_ACCOUNT_NUMBER` và `VIETQR_ACCOUNT_NAME`. Nếu chưa cấu hình, yêu cầu nạp vẫn được tạo nhưng giao diện chỉ hiển thị nội dung chuyển khoản. Giao dịch chỉ chuyển sang trạng thái chờ Admin sau khi khách bấm xác nhận đã chuyển tiền; không cần quay lại bot Telegram.

## Cấu hình sản phẩm mở rộng

Migration tự thêm các cột tương thích ngược vào `store`: `description`, `category`, `image_url`, `featured`, `warranty_days`, `active`, `purchases`. Admin cũ vẫn thêm được gói như trước, đồng thời có thể cập nhật toàn bộ metadata từ Trung tâm quản trị trong Mini App; không có dữ liệu sản phẩm mock trong production.

Provider ngoài dùng adapter generic và phải được Admin cấu hình đúng base URL/API key/product ID theo hợp đồng của nhà cung cấp. Hệ thống không tự đoán API. Checkout gọi provider ngoài transaction ghi SQLite; lỗi/timeout không trừ tiền. Webhook thanh toán chỉ hoạt động sau khi `PAYMENT_WEBHOOK_SECRET` được cấu hình và phía thanh toán gửi chữ ký HMAC đúng hợp đồng endpoint.

## Bảo mật checkout

Backend đọc lại giá từ SQLite, bắt đầu `BEGIN IMMEDIATE`, kiểm tra số dư, trừ tiền, cộng lượt, tạo lịch sử mua và xóa giỏ trong cùng transaction. Cặp `(user_id, idempotency_key)` là duy nhất để chống bấm mua nhiều lần. API chi tiết đơn luôn lọc đồng thời theo `id` và `user_id`.
