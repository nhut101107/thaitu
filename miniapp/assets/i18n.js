import {state} from "./state.js";

const EXACT_EN = new Map([
  ["Trang chủ", "Home"], ["Cửa hàng", "Store"], ["Đơn hàng", "Orders"],
  ["Tiện ích", "Tools"], ["Tài khoản", "Account"], ["Ngôn ngữ", "Language"],
  ["Tiếng Việt", "Vietnamese"], ["Tìm kiếm", "Search"], ["Thông báo", "Notifications"],
  ["Cài ứng dụng", "Install app"], ["Chào mừng trở lại", "Welcome back"],
  ["Thông báo từ Admin", "Admin announcement"], ["SỐ DƯ KHẢ DỤNG", "AVAILABLE BALANCE"],
  ["Tổng chi tiêu", "Total spent"], ["Nạp tiền", "Deposit"], ["Mua gói lượt Cookie", "Buy Cookie credits"],
  ["Công cụ từ bot", "Bot tools"], ["Hỗ trợ trực tiếp", "Live support"],
  ["Sản phẩm nổi bật", "Featured products"], ["Gợi ý phù hợp", "Recommended for you"],
  ["Xem tất cả →", "View all →"], ["Chưa có sản phẩm", "No products yet"],
  ["Admin chưa thêm gói vào cửa hàng.", "The admin has not added products yet."],
  ["Sản phẩm mới sẽ xuất hiện tại đây.", "New products will appear here."],
  ["Giao lượt tự động", "Automatic delivery"], ["Thanh toán an toàn", "Secure payment"],
  ["Khám phá sản phẩm", "Explore products"], ["Tất cả", "All"], ["Tất cả sản phẩm", "All products"],
  ["Phổ biến", "Popular"], ["Giá thấp", "Lowest price"], ["Giá cao", "Highest price"],
  ["Không tìm thấy", "Nothing found"], ["Thử từ khóa hoặc danh mục khác.", "Try another keyword or category."],
  ["Đơn hàng & bảo hành", "Orders & warranty"], ["Đã mua", "Purchased"], ["Còn bảo hành", "Under warranty"],
  ["Chưa có đơn hàng", "No orders yet"], ["Các gói đã mua sẽ xuất hiện ở đây.", "Your purchased products will appear here."],
  ["Khám phá cửa hàng", "Explore store"], ["Đăng nhập Netflix TV", "Netflix TV login"],
  ["Tạo link NFToken", "Create NFToken link"], ["Rút Cookie VIP", "Get VIP Cookie"],
  ["Cookie miễn phí", "Free Cookie"], ["Điểm danh Cookie Free", "Daily Cookie check-in"],
  ["Nhiệm vụ nhận thưởng", "Reward missions"], ["Giới thiệu bạn bè", "Invite friends"],
  ["Nhập mã quà tặng", "Redeem gift code"], ["Nạp tiền", "Deposit money"],
  ["Báo lỗi & hỗ trợ", "Report & support"], ["Hướng dẫn", "Help"], ["Tiện ích nhanh", "Quick tools"],
  ["Không rời Mini App", "Stay in the Mini App"], ["Kho Premium", "Premium stock"], ["Kho Free", "Free stock"],
  ["Tài khoản", "Account"], ["An toàn & bảo mật", "Safety & security"], ["Số dư ví", "Wallet balance"],
  ["Chạm để nạp", "Tap to deposit"], ["Lượt tạo link NFToken", "NFToken link credits"],
  ["Lượt lấy Cookie VIP", "VIP Cookie credits"], ["Lịch sử đơn hàng", "Order history"],
  ["Gửi yêu cầu ngay trong app", "Send a request in the app"], ["Chính sách & cam kết", "Policies & commitments"],
  ["Minh bạch gói dịch vụ", "Transparent plans"], ["Giao lượt tức thì", "Instant delivery"],
  ["Chỉ xử lý sau thanh toán", "Processed after payment"], ["Cookie của bạn", "Your Cookie"],
  ["Sao chép Cookie", "Copy Cookie"], ["Tải Cookie bảo mật", "Download secure Cookie"],
  ["Nhận Cookie ngay", "Get Cookie now"], ["Điểm danh hôm nay", "Check in today"],
  ["Thông tin tài khoản", "Account information"], ["Mã TV", "TV code"], ["Kiểm tra & kết nối", "Check & connect"],
  ["Đang xử lý...", "Processing..."], ["Thử lại", "Try again"], ["Đã sao chép", "Copied"],
]);

const PATTERN_EN = [
  [/^Xin chào,\s*(.+)$/, "Hello, $1"],
  [/^(\d+) đơn đã mua$/, "$1 orders purchased"],
  [/^(\d+) sản phẩm$/, "$1 products"],
  [/^(\d+) lượt đã mua$/, "$1 purchased credits"],
  [/^(\d+) lượt NFToken$/, "$1 NFToken credits"],
  [/^(\d+) lượt Cookie VIP$/, "$1 VIP Cookie credits"],
  [/^(\d+)\/(\d+) lượt hôm nay$/, "$1/$2 credits today"],
  [/^(\d+)\/(\d+) lượt còn lại hôm nay$/, "$1/$2 credits remaining today"],
  [/^(\d+) lượt hôm nay$/, "$1 credits today"],
  [/^(\d+) lượt còn lại hôm nay$/, "$1 credits remaining today"],
  [/^(\d+)\/5 người hợp lệ · nhận 2 lượt NFToken$/, "$1\/5 qualified referrals · get 2 NFToken credits"],
  [/^(.+) ngày BH$/, "$1-day warranty"],
  [/^còn (\d+)$/, "$1 left"],
];

export function isEnglish() {
  return state.bootstrap?.user?.language === "en";
}

export function translateText(value) {
  if (!isEnglish()) return value;
  const text = String(value);
  if (EXACT_EN.has(text)) return EXACT_EN.get(text);
  for (const [pattern, replacement] of PATTERN_EN) {
    if (pattern.test(text)) return text.replace(pattern, replacement);
  }
  return text;
}

export function translateDom(root = document) {
  if (!isEnglish() || !root) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let node;
  while ((node = walker.nextNode())) nodes.push(node);
  nodes.forEach((textNode) => {
    const parent = textNode.parentElement;
    if (!parent || parent.closest("script,style,textarea,code")) return;
    const translated = translateText(textNode.nodeValue.trim());
    if (translated !== textNode.nodeValue.trim()) {
      textNode.nodeValue = textNode.nodeValue.replace(textNode.nodeValue.trim(), translated);
    }
  });
}
