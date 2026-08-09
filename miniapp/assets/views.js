import {api} from "./api.js";
import {state, update} from "./state.js";
import {emptyState, escapeHtml, formatMoney, icon, modal, productCard, skeleton, toast} from "./components.js";

function section(title, body, action = "") {
  return `<section class="content-section"><div class="section-title"><div><small>NFToken Pro</small><h2>${title}</h2></div>${action}</div>${body}</section>`;
}

export function homeView() {
  const user = state.bootstrap.user;
  const featured = state.products.filter((item) => item.featured).slice(0, 4);
  const suggestions = (featured.length ? state.products.filter((item) => !item.featured) : state.products).slice(0, 4);
  const products = featured.length ? featured : state.products.slice(0, 4);
  return `<div class="page home-page">
    <div class="eyebrow">CHÀO MỪNG TRỞ LẠI</div><div class="welcome"><h1>Xin chào, ${escapeHtml(user.firstName)}<i>✦</i></h1><span>${escapeHtml(user.plan)}</span></div>
    <section class="balance-card"><div><small>SỐ DƯ KHẢ DỤNG</small><strong>${formatMoney(user.balance)}</strong></div><span>${user.credits} lượt Cookie VIP</span><hr><div class="balance-foot"><p>Tổng chi tiêu<b>${formatMoney(user.spent)}</b></p><button data-action="deposit">${icon("plus")} Nạp tiền</button></div></section>
    <div class="quick-grid">
      <button data-route="store"><i class="purple">${icon("store")}</i><b>Cửa hàng</b><small>Mua gói lượt Cookie</small>${icon("arrow")}</button>
      <button data-route="orders"><i class="blue">${icon("orders")}</i><b>Đơn hàng</b><small>${user.orderCount} đơn đã mua</small>${icon("arrow")}</button>
      <button data-action="cart"><i>${icon("cart")}</i><b>Giỏ hàng</b><small>${state.cart.count} sản phẩm</small>${icon("arrow")}</button>
      <button data-route="tools"><i class="mint">${icon("tools")}</i><b>Tiện ích</b><small>Công cụ từ bot</small>${icon("arrow")}</button>
    </div>
    <button class="social-card" data-action="support"><i>${icon("account")}</i><span><b>Hỗ trợ trực tiếp</b><small>${escapeHtml(state.bootstrap.support)}</small></span>${icon("arrow")}</button>
    ${section("Sản phẩm nổi bật", products.length ? `<div class="horizontal-products">${products.map(productCard).join("")}</div>` : emptyState("Chưa có sản phẩm", "Admin chưa thêm gói vào cửa hàng."), '<button data-route="store" class="text-button">Xem tất cả →</button>')}
    ${section("Gợi ý phù hợp", suggestions.length ? `<div class="horizontal-products">${suggestions.map(productCard).join("")}</div>` : emptyState("Chưa có gợi ý", "Sản phẩm mới sẽ xuất hiện tại đây."))}
    <div class="trust-strip"><span>⚡<b>Giao lượt tự động</b></span><span>♢<b>Thanh toán an toàn</b></span></div>
  </div>`;
}

export function storeView(loading = false) {
  const categoryButtons = ["", ...state.categories].map((value) => `<button data-category="${escapeHtml(value)}" class="${state.category === value ? "active" : ""}">${escapeHtml(value || "Tất cả")}</button>`).join("");
  return `<div class="page"><div class="eyebrow">NFTOKEN MARKET</div><div class="store-heading"><h1>Khám phá sản phẩm</h1><span>${state.products.length} SP</span></div>
    <label class="search-box">${icon("search")}<input id="product-search" value="${escapeHtml(state.search)}" placeholder="Tìm gói Cookie, Spotify..."><button data-clear-search>×</button></label>
    <div class="category-row">${categoryButtons}</div>
    <div class="list-heading"><b>Tất cả sản phẩm</b><select id="sort-products"><option value="popular">Phổ biến</option><option value="price_asc" ${state.sort === "price_asc" ? "selected" : ""}>Giá thấp</option><option value="price_desc" ${state.sort === "price_desc" ? "selected" : ""}>Giá cao</option></select></div>
    ${loading ? skeleton(6) : state.products.length ? `<div class="product-grid">${state.products.map(productCard).join("")}</div>` : emptyState("Không tìm thấy", "Thử từ khóa hoặc danh mục khác.")}
  </div>`;
}

export function ordersView(loading = false) {
  if (loading) return `<div class="page"><div class="eyebrow">KHO CỦA BẠN</div><h1>Đơn hàng & bảo hành</h1>${skeleton(3)}</div>`;
  const active = state.orders.filter((order) => order.warranty_until && new Date(order.warranty_until) > new Date()).length;
  const cards = state.orders.map((order) => `<article class="order-card" data-order="${order.id}"><header><i>${String(order.plan_name || "N")[0]}</i><div><h3>${escapeHtml(order.plan_name)}</h3><small>#NFT${String(order.id).padStart(6, "0")}</small></div><em>${escapeHtml(order.status || "COMPLETED")}</em></header><div class="order-meta"><span>NGÀY MUA<b>${escapeHtml(order.date)}</b></span><span>THANH TOÁN<b>${formatMoney(order.price)}</b></span><span>BẢO HÀNH<b>${order.warranty_until ? escapeHtml(order.warranty_until.split(" ")[0]) : "Không áp dụng"}</b></span></div><button>Xem chi tiết ${icon("arrow")}</button></article>`).join("");
  return `<div class="page"><div class="eyebrow">KHO CỦA BẠN</div><div class="store-heading"><h1>Đơn hàng & bảo hành</h1><button data-action="reload-orders">↻</button></div><div class="order-stats"><div><i>✓</i><small>Đã mua</small><b>${state.orders.length} đơn</b></div><div><i>♢</i><small>Còn bảo hành</small><b>${active} đơn</b></div></div>${cards || emptyState("Chưa có đơn hàng", "Các gói đã mua sẽ xuất hiện ở đây.", '<button class="button" data-route="store">Khám phá cửa hàng</button>')}</div>`;
}

export function toolsView() {
  const tools = [
    ["📺", "Đăng nhập Netflix TV", "Nhập mã TV qua bot", "menu_tv_log", true],
    ["⚡", "Tạo NFToken", "Lấy Cookie sống và link nhanh", "menu_chk", true],
    ["🎁", "Cookie miễn phí", "Nhận cookie theo hạn mức gói", "menu_freecookie", true],
    ["🔐", "Tạo mã 2FA", "Source bot hiện chưa có TOTP", "", false],
  ];
  return `<div class="page"><div class="eyebrow">NFTOKEN TOOLBOX</div><div class="store-heading"><h1>Tiện ích nhanh</h1><span>BOT NATIVE</span></div><div class="privacy-banner">${icon("shield")}<div><b>Kết nối chức năng thật</b><small>Tác vụ được chuyển về bot, không tạo chức năng giả.</small></div></div><div class="tool-grid">${tools.map(([glyph,name,desc,action,enabled]) => `<button ${enabled ? `data-bot-action="${action}"` : "disabled"}><i>${glyph}</i><b>${name}</b><small>${desc}</small>${!enabled ? "<em>Chưa khả dụng</em>" : ""}</button>`).join("")}</div><section class="panel"><h2>Cách sử dụng</h2><p>Chọn tiện ích để Mini App gửi yêu cầu an toàn về cuộc trò chuyện. Bot sẽ hiển thị luồng chức năng tương ứng mà không làm mất menu hiện tại.</p></section></div>`;
}

export function accountView() {
  const user = state.bootstrap.user;
  const initials = escapeHtml((user.firstName || "N")[0]);
  const avatar = user.photoUrl ? `<img src="${escapeHtml(user.photoUrl)}" alt="">` : initials;
  return `<div class="page account-page"><div class="profile-hero"><div class="profile-avatar">${avatar}</div><h1>${escapeHtml(`${user.firstName} ${user.lastName}`.trim())}</h1><p>${user.username ? `@${escapeHtml(user.username)}` : "Chưa đặt username"}</p><span>♕ ${escapeHtml(user.plan)}</span></div>
    <section class="panel account-list"><header><h2>Tài khoản</h2><small>An toàn & bảo mật</small></header><button><i>${icon("wallet")}</i><span><b>Số dư ví</b><small>${formatMoney(user.balance)}</small></span></button><button data-route="orders"><i>${icon("orders")}</i><span><b>Lịch sử đơn hàng</b><small>${user.orderCount} đơn đã mua</small></span>${icon("arrow")}</button><button data-action="support"><i>${icon("account")}</i><span><b>Hỗ trợ trực tiếp</b><small>${escapeHtml(state.bootstrap.support)}</small></span>${icon("arrow")}</button></section>
    <section class="panel commitments"><header><h2>Chính sách & cam kết</h2></header><p>♢ <span><b>Minh bạch gói dịch vụ</b><small>Thông tin lượt và giá được đọc trực tiếp từ hệ thống.</small></span></p><p>⚡ <span><b>Giao lượt tức thì</b><small>Lượt Cookie được cộng sau khi giao dịch thành công.</small></span></p><p>▣ <span><b>Chỉ xử lý sau thanh toán</b><small>Backend kiểm tra lại giá và số dư trong một transaction.</small></span></p></section>
    <section class="panel membership"><h2>Hạng ${escapeHtml(user.plan)}</h2><p>Telegram ID: ${user.id}</p><div><i style="width:${Math.min(100, user.spent / 10000)}%"></i></div></section><footer class="version">NFTOKEN PRO MINI APP · VERSION 1.0</footer></div>`;
}

export async function openProduct(id) {
  try {
    const {item} = await api.product(id);
    modal(`<div class="detail-image">${item.imageUrl ? `<img src="${escapeHtml(item.imageUrl)}" alt="">` : '<span class="brand-mark">N</span>'}</div><div class="eyebrow">${escapeHtml(item.category)}</div><h2>${escapeHtml(item.name)}</h2><p>${escapeHtml(item.description)}</p><dl><div><dt>Giá</dt><dd>${formatMoney(item.price)}</dd></div><div><dt>Quyền lợi</dt><dd>${item.credits} lượt Cookie VIP</dd></div><div><dt>Bảo hành</dt><dd>${item.warrantyDays ? `${item.warrantyDays} ngày` : "Không áp dụng"}</dd></div></dl><button class="button wide" data-modal-add="${item.id}">Thêm vào giỏ · ${formatMoney(item.price)}</button>`, {onOpen(root, close) { root.querySelector("[data-modal-add]").onclick = async () => { await addToCart(item.id); close(); }; }});
  } catch (error) { toast(error.message, "error"); }
}

export async function addToCart(id) {
  const current = state.cart.items.find((item) => item.id === Number(id));
  try {
    const cart = await api.setCart(id, (current?.quantity || 0) + 1);
    update({cart}); toast("Đã thêm vào giỏ");
  } catch (error) { toast(error.message, "error"); }
}

export function openCart() {
  const content = state.cart.items.length ? `${state.cart.items.map((item) => `<div class="cart-line"><div><b>${escapeHtml(item.name)}</b><small>${formatMoney(item.price)}</small></div><div><button data-qty="${item.id}" data-value="${item.quantity - 1}">−</button><span>${item.quantity}</span><button data-qty="${item.id}" data-value="${item.quantity + 1}">+</button></div></div>`).join("")}<div class="cart-total"><span>Tổng thanh toán</span><b>${formatMoney(state.cart.total)}</b></div><button class="button wide" data-checkout>Xác nhận mua hàng</button>` : emptyState("Giỏ hàng trống", "Thêm sản phẩm từ cửa hàng để tiếp tục.");
  modal(`<h2>Giỏ hàng</h2>${content}`, {onOpen(root, close) {
    root.querySelectorAll("[data-qty]").forEach((button) => button.onclick = async () => { try { const cart = await api.setCart(button.dataset.qty, Number(button.dataset.value)); update({cart}); close(); openCart(); } catch (error) { toast(error.message, "error"); } });
    const checkout = root.querySelector("[data-checkout]"); if (checkout) checkout.onclick = () => confirmCheckout(close);
  }});
}

function confirmCheckout(closeCart) {
  modal(`<div class="confirm-icon">${icon("shield")}</div><h2>Xác nhận thanh toán?</h2><p>Backend sẽ kiểm tra lại giá, sản phẩm và số dư trước khi tạo đơn.</p><div class="cart-total"><span>Tổng cộng</span><b>${formatMoney(state.cart.total)}</b></div><button class="button wide" data-confirm-checkout>Mua ngay</button>`, {onOpen(root, close) { root.querySelector("[data-confirm-checkout]").onclick = async (event) => {
    if (state.busy) return; state.busy = true; event.currentTarget.disabled = true;
    try { const key = crypto.randomUUID().replaceAll("-", ""); const result = await api.checkout(key); update({cart:{items:[],count:0,total:0}}); close(); closeCart(); toast(`Thanh toán thành công ${formatMoney(result.total)}`); }
    catch (error) { toast(error.message, "error"); event.currentTarget.disabled = false; }
    finally { state.busy = false; }
  }; }});
}

export function openOrder(id) {
  api.order(id).then(({item}) => modal(`<div class="confirm-icon">${icon("orders")}</div><div class="eyebrow">ĐƠN #NFT${String(item.id).padStart(6,"0")}</div><h2>${escapeHtml(item.plan_name)}</h2><dl><div><dt>Ngày mua</dt><dd>${escapeHtml(item.date)}</dd></div><div><dt>Số lượng</dt><dd>${item.quantity || 1}</dd></div><div><dt>Thanh toán</dt><dd>${formatMoney(item.price)}</dd></div><div><dt>Trạng thái</dt><dd>${escapeHtml(item.status)}</dd></div><div><dt>Bảo hành</dt><dd>${item.warranty_until ? escapeHtml(item.warranty_until) : "Không áp dụng"}</dd></div></dl>`)).catch((error) => toast(error.message,"error"));
}
