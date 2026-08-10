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
    ${state.bootstrap.announcement ? `<div class="announcement-banner"><i>📣</i><p><b>Thông báo từ Admin</b><span>${escapeHtml(state.bootstrap.announcement)}</span></p></div>` : ""}
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
  const quota = state.tools?.quota || state.bootstrap.quota || {};
  const stock = state.tools?.stock || {premium: state.bootstrap.inventory.premiumCookies, free: 0};
  const flags = state.tools?.features || state.bootstrap.features || {};
  const tools = [
    ["📺", "Đăng nhập Netflix TV", "Nhập mã TV và xử lý ngay trong app", "tv", flags.tv],
    ["⚡", "Tạo NFToken theo gói", `${quota.tokensUsed || 0}/${quota.tokensMax || 0} lượt hôm nay`, "plan-token", flags.planToken],
    ["🍪", "Rút Cookie VIP", `${quota.credits || 0} lượt đã mua`, "vip-token", flags.vipToken],
    ["🎁", "Cookie miễn phí", `${quota.freeCookiesUsed || 0}/${quota.freeCookiesMax || 0} lượt hôm nay`, "free-cookie", flags.freeCookie],
    ["🎟", "Nhập mã quà tặng", "Cộng số dư trực tiếp", "giftcode", flags.giftcode],
    ["💸", "Nạp tiền", "Tạo QR và yêu cầu duyệt", "deposit", flags.deposit],
    ["🛟", "Báo lỗi & hỗ trợ", "Gửi thẳng yêu cầu đến Admin", "support", flags.support],
    ["📚", "Hướng dẫn", "Xem cách sử dụng các chức năng", "help", true],
  ];
  return `<div class="page"><div class="eyebrow">NFTOKEN TOOLBOX</div><div class="store-heading"><h1>Tiện ích nhanh</h1><span>TRỰC TIẾP</span></div><div class="privacy-banner">${icon("shield")}<div><b>Không rời Mini App</b><small>Mọi tác vụ bên dưới được xác thực và xử lý trực tiếp.</small></div></div><div class="tool-stock"><span>Kho Premium <b>${stock.premium || 0}</b></span><span>Kho Free <b>${stock.free || 0}</b></span></div><div class="tool-grid">${tools.map(([glyph,name,desc,action,enabled]) => `<button data-tool="${action}" ${enabled === false ? "disabled" : ""}><i>${glyph}</i><b>${name}</b><small>${enabled === false ? "Admin đang tạm tắt chức năng" : desc}</small>${enabled === false ? "<em>Tạm tắt</em>" : ""}</button>`).join("")}</div></div>`;
}

export function accountView() {
  const user = state.bootstrap.user;
  const initials = escapeHtml((user.firstName || "N")[0]);
  const avatar = user.photoUrl ? `<img src="${escapeHtml(user.photoUrl)}" alt="">` : initials;
  return `<div class="page account-page"><div class="profile-hero"><div class="profile-avatar">${avatar}</div><h1>${escapeHtml(`${user.firstName} ${user.lastName}`.trim())}</h1><p>${user.username ? `@${escapeHtml(user.username)}` : "Chưa đặt username"}</p><span>♕ ${escapeHtml(user.plan)}</span></div>
    <section class="panel account-list"><header><h2>Tài khoản</h2><small>An toàn & bảo mật</small></header><button data-action="deposit"><i>${icon("wallet")}</i><span><b>Số dư ví</b><small>${formatMoney(user.balance)} · Chạm để nạp</small></span>${icon("arrow")}</button><button data-route="orders"><i>${icon("orders")}</i><span><b>Lịch sử đơn hàng</b><small>${user.orderCount} đơn đã mua</small></span>${icon("arrow")}</button><button data-action="support"><i>${icon("account")}</i><span><b>Hỗ trợ trực tiếp</b><small>Gửi yêu cầu ngay trong app</small></span>${icon("arrow")}</button></section>
    ${state.bootstrap.isAdmin ? `<button class="admin-entry" data-route="admin"><i>♛</i><span><b>Trung tâm quản trị</b><small>Quản lý toàn bộ hệ thống ngay trong Mini App</small></span>${icon("arrow")}</button>` : ""}
    <section class="panel commitments"><header><h2>Chính sách & cam kết</h2></header><p>♢ <span><b>Minh bạch gói dịch vụ</b><small>Thông tin lượt và giá được đọc trực tiếp từ hệ thống.</small></span></p><p>⚡ <span><b>Giao lượt tức thì</b><small>Lượt Cookie được cộng sau khi giao dịch thành công.</small></span></p><p>▣ <span><b>Chỉ xử lý sau thanh toán</b><small>Backend kiểm tra lại giá và số dư trong một transaction.</small></span></p></section>
    <section class="panel membership"><h2>Hạng ${escapeHtml(user.plan)}</h2><p>Telegram ID: ${user.id}</p><div><i style="width:${Math.min(100, user.spent / 10000)}%"></i></div></section></div>`;
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

function syncQuota(quota) {
  if (!quota) return;
  state.tools = {...(state.tools || {}), quota};
  state.bootstrap.quota = quota;
  state.bootstrap.user.credits = quota.credits;
}

function busyButton(button, text = "Đang xử lý...") {
  const old = button.textContent;
  button.disabled = true;
  button.textContent = text;
  return () => { button.disabled = false; button.textContent = old; };
}

function accountSummary(account = {}) {
  return `<dl><div><dt>Email</dt><dd>${escapeHtml(account.email || "Không rõ")}</dd></div><div><dt>Gói</dt><dd>${escapeHtml(account.plan || "Không rõ")}</dd></div><div><dt>Quốc gia</dt><dd>${escapeHtml(account.country || "Không rõ")}</dd></div><div><dt>Trạng thái</dt><dd>${escapeHtml(account.status || "Không rõ")}</dd></div></dl>`;
}

export function openNftoken(mode = "plan") {
  const vip = mode === "vip";
  modal(`<div class="confirm-icon">${vip ? "🍪" : "⚡"}</div><h2>${vip ? "Rút Cookie VIP" : "Tạo NFToken theo gói"}</h2><p>${vip ? "Lượt đã mua sẽ chỉ bị trừ khi tạo thành công." : "Sử dụng hạn mức NFToken hằng ngày của gói hiện tại."}</p>${vip ? '<label class="field">Số lượng<input id="tool-quantity" type="number" min="1" max="5" value="1"></label>' : ""}<button class="button wide" data-run-nftoken>Bắt đầu xử lý</button>`, {onOpen(root, close) {
    root.querySelector("[data-run-nftoken]").onclick = async (event) => {
      const done = busyButton(event.currentTarget); const quantity = Number(root.querySelector("#tool-quantity")?.value || 1);
      try {
        const result = await api.nftoken(mode, quantity); syncQuota(result.quota); close();
        modal(`<div class="confirm-icon">✓</div><h2>Tạo thành công ${result.items.length} NFToken</h2>${result.items.map((item, index) => `<article class="token-result"><b>Tài khoản ${index + 1}</b>${accountSummary(item.account)}<a class="button wide" href="${escapeHtml(item.link)}" target="_blank" rel="noopener">Mở Netflix</a><button class="button secondary wide" data-copy-link="${escapeHtml(item.link)}">Sao chép link</button></article>`).join("")}`, {onOpen(resultRoot) { resultRoot.querySelectorAll("[data-copy-link]").forEach((button) => button.onclick = () => navigator.clipboard.writeText(button.dataset.copyLink).then(() => toast("Đã sao chép link"))); }});
      } catch (error) { toast(error.message, "error"); done(); }
    };
  }});
}

export function claimFreeCookie() {
  modal(`<div class="confirm-icon">🎁</div><h2>Cookie miễn phí</h2><p>Cookie được lấy theo đúng hạn mức gói của bạn.</p><button class="button wide" data-claim-free>Nhận Cookie ngay</button>`, {onOpen(root, close) {
    root.querySelector("[data-claim-free]").onclick = async (event) => {
      const done = busyButton(event.currentTarget);
      try { const result = await api.freeCookie(); syncQuota(result.quota); close(); modal(`<div class="confirm-icon">✓</div><h2>Cookie của bạn</h2><textarea class="result-text" readonly>${escapeHtml(result.cookie)}</textarea><button class="button wide" data-copy-cookie>Sao chép Cookie</button>`, {onOpen(resultRoot) { resultRoot.querySelector("[data-copy-cookie]").onclick = () => navigator.clipboard.writeText(result.cookie).then(() => toast("Đã sao chép Cookie")); }}); }
      catch (error) { toast(error.message, "error"); done(); }
    };
  }});
}

export function openTvLogin() {
  modal(`<div class="confirm-icon">📺</div><h2>Đăng nhập Netflix TV</h2><p>Mở Netflix trên TV, chọn đăng nhập từ trang web rồi nhập mã đang hiển thị.</p><label class="field">Mã TV<input id="tv-code" inputmode="text" maxlength="12" placeholder="Ví dụ: 12345678"></label><button class="button wide" data-run-tv>Kết nối TV</button>`, {onOpen(root, close) {
    root.querySelector("[data-run-tv]").onclick = async (event) => {
      const code = root.querySelector("#tv-code").value.trim(); const done = busyButton(event.currentTarget, "Đang kết nối, vui lòng chờ...");
      try { const result = await api.tvLogin(code); close(); modal(`<div class="confirm-icon">✓</div><h2>${escapeHtml(result.message)}</h2>${accountSummary(result.account)}`); }
      catch (error) { toast(error.message, "error"); done(); }
    };
  }});
}

export function openGiftcode() {
  modal(`<div class="confirm-icon">🎟</div><h2>Nhập mã quà tặng</h2><label class="field">Mã của bạn<input id="gift-code" maxlength="50" autocomplete="off" placeholder="SALE50K"></label><button class="button wide" data-redeem-code>Nhận quà</button>`, {onOpen(root, close) {
    root.querySelector("[data-redeem-code]").onclick = async (event) => {
      const done = busyButton(event.currentTarget);
      try { const result = await api.giftcode(root.querySelector("#gift-code").value); state.bootstrap.user.balance += result.amount; close(); toast(`Đã cộng ${formatMoney(result.amount)}`); update({bootstrap: state.bootstrap}); }
      catch (error) { toast(error.message, "error"); done(); }
    };
  }});
}

export function openDeposit() {
  modal(`<div class="confirm-icon">💸</div><h2>Nạp tiền</h2><label class="field">Số tiền<input id="deposit-amount" type="number" min="10000" max="100000000" step="10000" value="50000"></label><button class="button wide" data-create-deposit>Tạo yêu cầu nạp</button>`, {onOpen(root, close) {
    root.querySelector("[data-create-deposit]").onclick = async (event) => {
      const done = busyButton(event.currentTarget);
      try { const result = await api.deposit(Number(root.querySelector("#deposit-amount").value)); close(); modal(`<div class="eyebrow">GIAO DỊCH #${result.transactionId}</div><h2>Chuyển khoản ${formatMoney(result.amount)}</h2>${result.qrUrl ? `<img class="deposit-qr" src="${escapeHtml(result.qrUrl)}" alt="QR chuyển khoản">` : '<div class="privacy-banner">Admin chưa cấu hình QR ngân hàng. Hãy dùng nội dung bên dưới khi chuyển khoản.</div>'}<p>Nội dung chuyển khoản</p><div class="copy-value"><code>${escapeHtml(result.transferNote)}</code><button data-copy-note>${icon("copy")}</button></div><p class="muted">Yêu cầu đã gửi đến Admin để duyệt.</p>`, {onOpen(resultRoot) { resultRoot.querySelector("[data-copy-note]").onclick = () => navigator.clipboard.writeText(result.transferNote).then(() => toast("Đã sao chép nội dung")); }}); }
      catch (error) { toast(error.message, "error"); done(); }
    };
  }});
}

export function openSupport() {
  modal(`<div class="confirm-icon">🛟</div><h2>Báo lỗi & hỗ trợ</h2><label class="field">Mô tả vấn đề<textarea id="support-message" maxlength="1500" placeholder="Hãy mô tả chi tiết lỗi bạn gặp..."></textarea></label><button class="button wide" data-send-support>Gửi đến Admin</button>`, {onOpen(root, close) {
    root.querySelector("[data-send-support]").onclick = async (event) => {
      const done = busyButton(event.currentTarget);
      try { const result = await api.support(root.querySelector("#support-message").value); close(); toast(`Đã gửi yêu cầu #${result.ticketId}`); }
      catch (error) { toast(error.message, "error"); done(); }
    };
  }});
}

export function openHelp() {
  modal(`<div class="confirm-icon">📚</div><h2>Hướng dẫn sử dụng</h2><div class="help-list"><p><b>NFToken theo gói</b><small>Dùng hạn mức hằng ngày của gói thành viên.</small></p><p><b>Cookie VIP</b><small>Dùng lượt đã mua trong cửa hàng; chỉ trừ khi thành công.</small></p><p><b>Đăng nhập TV</b><small>Mã TV có thời hạn ngắn, hãy nhập ngay khi TV hiển thị.</small></p><p><b>Nạp tiền</b><small>Chuyển đúng số tiền và nội dung; Admin duyệt trực tiếp từ Telegram.</small></p></div>`);
}
