# Tiến độ Auto Clicker / Mini-game

## Mục tiêu

Tool chạy trên Windows desktop để hỗ trợ kiểm thử mini-game trên server test riêng. Không sửa hoặc xóa Telegram bot, Mini App hay database hiện có.

## Đã hoàn thành

- Có bản GUI một file: `dist/AutoRedClicker.exe`.
- Có source lõi: `auto_red_clicker.py`.
- Có menu GUI: `auto_red_clicker_app.py`.
- Có các chế độ:
  - **Auto click:** quét vùng 400x400 ở giữa virtual desktop, nhận diện hai dải màu đỏ HSV, lọc nhiễu, bỏ contour dưới 20 pixel, ưu tiên mục tiêu lớn và gần tâm; không thấy đỏ thì click tâm vùng quét.
  - **Dry-run:** chỉ chụp/nhận diện và ghi log, không di chuyển hoặc click chuột.
  - **Cưa gỗ:** quét nội dung các cửa sổ đang hiển thị, không phụ thuộc title/tab; nhận diện prompt A-Z và tự nhấn phím cho tới khi prompt biến mất.
  - **Đập đá:** giống cưa gỗ, có thêm nhận diện kiểu ô prompt màu xanh như video mẫu.
- F10 bật/tắt phiên đang chạy; F12 thoát khẩn cấp; Ctrl+C thoát an toàn.
- Có đếm ngược 3 giây, giới hạn thời gian mặc định 10 phút, giới hạn số lần click/nhấn và khoảng nghỉ mặc định 0.15 giây.
- Có log trạng thái, dọn hotkey, xử lý lỗi MSS/desktop không hoạt động.
- Chế độ cưa gỗ/đập đá tự quét cửa sổ đủ lớn, đưa cửa sổ có prompt lên foreground rồi mới nhấn.

## Cách dùng nhanh trên máy khác

1. Tải và mở `dist/AutoRedClicker.exe` trên Windows desktop đang đăng nhập, không chạy dưới service/RDP không có desktop.
2. Chọn **Cưa gỗ** hoặc **Đập đá**.
3. Tích **Bật chức năng** để chạy ngay; bỏ tích để dừng.
4. Không cần nhập title/tab khi dùng hai chế độ mini-game.
5. Dùng F12 nếu cần dừng khẩn cấp.

File `.exe` đã đóng gói Python và các thư viện runtime. Nếu chạy bằng source thì cài:

```powershell
python -m pip install -r requirements_auto_clicker.txt
python auto_red_clicker.py --dry-run
```

## Kiểm thử đã chạy

- `python -m py_compile auto_red_clicker.py auto_red_clicker_app.py`: đạt.
- Import source và parser `--stone-mode`: đạt.
- Nhận diện prompt mô phỏng chữ `E` trên ô đen và ô xanh: đạt.
- Build PyInstaller one-file: đạt; file đầu ra là `dist/AutoRedClicker.exe`.
- Dry-run trong môi trường hiện tại không click/nhấn; MSS dừng rõ ràng vì phiên hiện tại không có desktop tương tác.

## Việc còn lại cho phiên sau

1. Thêm giám sát balo: tự mở Tab, nhận diện trạng thái đủ `10kg`, phát âm báo/thông báo, tắt đèn trạng thái và dừng cưa/đập đá.
2. Thêm nút bật/tắt giám sát balo và đèn trạng thái trong menu: xanh = đang chạy, vàng = đang quét, đỏ = balo đầy; khi đầy thì đèn chạy tắt.
3. Xem video tuyến đường về NPC và thao tác bán hàng để đánh giá có thể tích hợp tự về/bán hay chỉ dừng để người dùng tự đi.
4. Sau khi thêm phần trên, chạy lại compile/import, dry-run không thao tác thật, build `.exe` và cập nhật file này.

## Dữ liệu cần cho chức năng balo/đường về

Không cần gửi toàn màn hình hoặc thông tin riêng. Chỉ cần ảnh/video crop phần giao diện:

- Balo gần đầy và balo đúng `10kg`.
- Bản đồ/waypoint từ khu đập đá về NPC.
- Đoạn thao tác bán hàng ở NPC.

## Quy tắc tiếp tục phát triển

- Chỉ sửa các file auto-clicker và file tiến độ liên quan.
- Không sửa/xóa file Telegram bot, Mini App hoặc database.
- Không thêm tính năng né/bypass anti-cheat.
- Luôn giữ F12, giới hạn thời gian và chế độ dry-run.
