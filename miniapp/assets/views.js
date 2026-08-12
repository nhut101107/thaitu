import {api} from "./api.js?v=18";
import {state, update} from "./state.js";
import {copyText, emptyState, escapeHtml, formatMoney, icon, modal, productCard, skeleton, toast} from "./components.js?v=18";

function section(title, body, action = "") {
  return `<section class="content-section"><div class="section-title"><div><small>Shop MMO</small><h2>${title}</h2></div>${action}</div>${body}</section>`;
}

export function homeView() {
  const user = state.bootstrap.user;
  const featured = state.products.filter((item) => item.featured).slice(0, 4);
  const suggestions = (featured.length ? state.products.filter((item) => !item.featured) : state.products).slice(0, 4);
  const products = featured.length ? featured : state.products.slice(0, 4);
  return `<div class="page home-page">
    <div class="eyebrow">CHÀO MỪNG TRỞ LẠI</div><div class="welcome"><h1>Xin chào, ${escapeHtml(user.firstName)}<i>✦</i></h1><span>${escapeHtml(user.plan)}</span></div>
    ${state.bootstrap.announcement ? `<div class="announcement-banner"><i>📣</i><p><b>Thông báo từ Admin</b><span>${escapeHtml(state.bootstrap.announcement)}</span></p></div>` : ""}
    <section class="balance-card"><div><small>SỐ DƯ KHẢ DỤNG</small><strong>${formatMoney(user.balance)}</strong></div><span>⚡ ${user.nftokenCredits || 0} lượt NFToken · 🍪 ${user.credits} lượt Cookie VIP</span><hr><div class="balance-foot"><p>Tổng chi tiêu<b>${formatMoney(user.spent)}</b></p><button data-action="deposit">${icon("plus")} Nạp tiền</button></div></section>
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
  return `<div class="page"><div class="eyebrow">SHOP MMO MARKET</div><div class="store-heading"><h1>Khám phá sản phẩm</h1><span>${state.products.length} SP</span></div>
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
  const trial = quota.trial || {};
  const stock = state.tools?.stock || {premium: state.bootstrap.inventory.premiumCookies, free: 0};
  const flags = state.tools?.features || state.bootstrap.features || {};
  const nftokenDescription = trial.nftokenEnabled && trial.nftokenDailyLimit > 0
    ? `Trải nghiệm còn ${trial.nftokenRemaining || 0}/${trial.nftokenDailyLimit || 0} lượt hôm nay`
    : `${quota.nftokenCredits || 0} lượt đã mua · ${Math.max(0,(quota.tokensMax || 0)-(quota.tokensUsed || 0))} lượt gói/ngày`;
  const cookieDescription = trial.cookieEnabled && trial.cookieDailyLimit > 0
    ? `Trải nghiệm còn ${trial.cookieRemaining || 0}/${trial.cookieDailyLimit || 0} lượt hôm nay`
    : `${quota.credits || 0} lượt đã mua`;
  const tools = [
    ["📺", "Đăng nhập Netflix TV", "Nhập mã TV và xử lý ngay trong app", "tv", flags.tv],
    ["⚡", "Tạo link NFToken", nftokenDescription, "plan-token", flags.planToken],
    ["🍪", "Rút Cookie VIP", cookieDescription, "vip-token", flags.vipToken],
    ["🎁", "Cookie miễn phí", `${quota.freeCookiesUsed || 0}/${quota.freeCookiesMax || 0} lượt hôm nay`, "free-cookie", flags.freeCookie],
    ["✅", "Điểm danh Cookie Free", `${state.bootstrap.checkin?.remaining || 0}/${state.bootstrap.checkin?.daily || 2} lượt còn lại hôm nay`, "checkin", true],
    ["🎯", "Nhiệm vụ nhận thưởng", "Hoàn thành nhiệm vụ để nhận lượt NFToken", "missions", true],
    ["🔗", "Giới thiệu bạn bè", `${state.bootstrap.referral?.count || 0}/5 người hợp lệ · nhận 2 lượt NFToken`, "referral", true],
    ["🎟", "Nhập mã quà tặng", "Cộng số dư trực tiếp", "giftcode", flags.giftcode],
    ["💸", "Nạp tiền", "Tạo QR và yêu cầu duyệt", "deposit", flags.deposit],
    ["🛟", "Báo lỗi & hỗ trợ", "Gửi thẳng yêu cầu đến Admin", "support", flags.support],
    ["📚", "Hướng dẫn", "Xem cách sử dụng các chức năng", "help", true],
  ];
  return `<div class="page"><div class="eyebrow">SHOP MMO SERVICES</div><div class="store-heading"><h1>Tiện ích nhanh</h1><span>TRỰC TIẾP</span></div><div class="privacy-banner">${icon("shield")}<div><b>Không rời Mini App</b><small>Mọi tác vụ bên dưới được xác thực và xử lý trực tiếp.</small></div></div><div class="tool-stock"><span>Kho Premium <b>${stock.premium || 0}</b></span><span>Kho Free <b>${stock.free || 0}</b></span></div><div class="tool-grid">${tools.map(([glyph,name,desc,action,enabled]) => `<button data-tool="${action}" ${enabled === false ? "disabled" : ""}><i>${glyph}</i><b>${name}</b><small>${enabled === false ? "Admin đang tạm tắt chức năng" : desc}</small>${enabled === false ? "<em>Tạm tắt</em>" : ""}</button>`).join("")}</div></div>`;
}

export function accountView() {
  const user = state.bootstrap.user;
  const initials = escapeHtml((user.firstName || "N")[0]);
  const avatar = user.photoUrl ? `<img src="${escapeHtml(user.photoUrl)}" alt="">` : initials;
  return `<div class="page account-page"><div class="profile-hero"><div class="profile-avatar">${avatar}</div><h1>${escapeHtml(`${user.firstName} ${user.lastName}`.trim())}</h1><p>${user.username ? `@${escapeHtml(user.username)}` : "Chưa đặt username"}</p><span>♕ ${escapeHtml(user.plan)}</span></div>
    <section class="panel account-list"><header><h2>Tài khoản</h2><small>An toàn & bảo mật</small></header><button data-action="deposit"><i>${icon("wallet")}</i><span><b>Số dư ví</b><small>${formatMoney(user.balance)} · Chạm để nạp</small></span>${icon("arrow")}</button><button><i>⚡</i><span><b>Lượt tạo link NFToken</b><small>${user.nftokenCredits || 0} lượt đã mua</small></span></button><button><i>🍪</i><span><b>Lượt lấy Cookie VIP</b><small>${user.credits} lượt đã mua</small></span></button><button data-route="orders"><i>${icon("orders")}</i><span><b>Lịch sử đơn hàng</b><small>${user.orderCount} đơn đã mua</small></span>${icon("arrow")}</button><button data-action="support"><i>${icon("account")}</i><span><b>Hỗ trợ trực tiếp</b><small>Gửi yêu cầu ngay trong app</small></span>${icon("arrow")}</button></section>
    <section class="panel language-panel"><label class="field">Ngôn ngữ<select data-language><option value="vi" ${user.language === "vi" ? "selected" : ""}>Tiếng Việt</option><option value="en" ${user.language === "en" ? "selected" : ""}>English</option></select></label></section>
    ${state.bootstrap.isAdmin ? `<button class="admin-entry" data-route="admin"><i>♛</i><span><b>Trung tâm quản trị</b><small>Quản lý toàn bộ hệ thống ngay trong Mini App</small></span>${icon("arrow")}</button>` : ""}
    <section class="panel commitments"><header><h2>Chính sách & cam kết</h2></header><p>♢ <span><b>Minh bạch gói dịch vụ</b><small>Thông tin lượt và giá được đọc trực tiếp từ hệ thống.</small></span></p><p>⚡ <span><b>Giao lượt tức thì</b><small>Lượt Cookie được cộng sau khi giao dịch thành công.</small></span></p><p>▣ <span><b>Chỉ xử lý sau thanh toán</b><small>Backend kiểm tra lại giá và số dư trong một transaction.</small></span></p></section>
    <section class="panel membership"><h2>Hạng ${escapeHtml(user.plan)}</h2><p>Telegram ID: ${user.id}</p><div><i style="width:${Math.min(100, user.spent / 10000)}%"></i></div></section></div>`;
}

export async function openProduct(id) {
  try {
    const {item} = await api.product(id);
    modal(`<div class="detail-image">${item.imageUrl ? `<img src="${escapeHtml(item.imageUrl)}" alt="">` : '<span class="brand-mark">N</span>'}</div><div class="eyebrow">${escapeHtml(item.category)}</div><h2>${escapeHtml(item.name)}</h2><p>${escapeHtml(item.description)}</p><dl><div><dt>Giá</dt><dd>${formatMoney(item.price)}</dd></div><div><dt>⚡ Tạo link NFToken</dt><dd>${item.nftokenCredits || 0} lượt</dd></div><div><dt>🍪 Lấy Cookie VIP</dt><dd>${item.credits} lượt</dd></div><div><dt>Bảo hành</dt><dd>${item.warrantyDays ? `${item.warrantyDays} ngày` : "Không áp dụng"}</dd></div></dl><button class="button wide" data-modal-add="${item.id}">Thêm vào giỏ · ${formatMoney(item.price)}</button>`, {onOpen(root, close) { root.querySelector("[data-modal-add]").onclick = async () => { await addToCart(item.id); close(); }; }});
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
  modal(`<div class="confirm-icon">${icon("shield")}</div><h2>Xác nhận thanh toán?</h2><p>Backend sẽ kiểm tra lại giá, sản phẩm và số dư trước khi tạo đơn.</p><label class="field">Mã giảm giá (không bắt buộc)<input id="checkout-promo" maxlength="50" autocomplete="off" placeholder="Nhập mã giảm giá"></label><div class="cart-total"><span>Tổng cộng</span><b>${formatMoney(state.cart.total)}</b></div><button class="button wide" data-confirm-checkout>Mua ngay</button>`, {onOpen(root, close) { root.querySelector("[data-confirm-checkout]").onclick = async (event) => {
    if (state.busy) return; state.busy = true; event.currentTarget.disabled = true;
    try { const key = crypto.randomUUID().replaceAll("-", ""); const promoCode = root.querySelector("#checkout-promo")?.value.trim() || ""; const result = await api.checkout(key, promoCode); update({cart:{items:[],count:0,total:0}}); close(); closeCart(); toast(`Thanh toán thành công ${formatMoney(result.total)}`); }
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
  state.bootstrap.user.nftokenCredits = quota.nftokenCredits;
}

function busyButton(button, text = "Đang xử lý...") {
  const old = button.textContent;
  button.disabled = true;
  button.textContent = text;
  return () => { button.disabled = false; button.textContent = old; };
}

function legacyAccountSummary(account = {}) {
  return `<dl><div><dt>Email</dt><dd>${escapeHtml(account.email || "Netflix không cung cấp")}</dd></div><div><dt>Gói</dt><dd>${escapeHtml(account.plan || "Netflix không cung cấp")}</dd></div><div><dt>Quốc gia</dt><dd>${escapeHtml(account.country || "Netflix không cung cấp")}</dd></div><div><dt>Trạng thái</dt><dd>${escapeHtml(account.status || "Netflix không cung cấp")}</dd></div></dl>`;
}

function accountSummary(account = {}) {
  const rows = Array.isArray(account.details) && account.details.length
    ? account.details
    : [
        {label: "Email", value: account.email},
        {label: "Gói", value: account.plan},
        {label: "Quốc gia", value: account.country},
        {label: "Trạng thái", value: account.status},
      ];
  return `<section class="account-summary"><div class="account-summary-head"><b>Thông tin tài khoản</b></div><div class="account-detail-grid">${rows.map((row) => `<div class="account-detail"><span>${escapeHtml(row.label || "Thông tin")}</span><strong>${escapeHtml(row.value || "Netflix không cung cấp")}</strong></div>`).join("")}</div></section>`;
}

export function openNftoken(mode = "plan") {
  const vip = mode === "vip";
  const trial = (state.tools?.quota || state.bootstrap.quota || {}).trial || {};
  const trialActive = vip
    ? trial.cookieEnabled && trial.cookieDailyLimit > 0
    : trial.nftokenEnabled && trial.nftokenDailyLimit > 0;
  const trialRemaining = vip ? trial.cookieRemaining : trial.nftokenRemaining;
  const requestId = globalThis.crypto?.randomUUID?.() || `nftoken-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const usageText = trialActive && trialRemaining > 0
    ? `Bạn còn ${trialRemaining} lượt trải nghiệm hôm nay. Lượt trải nghiệm được dùng trước.`
    : vip ? "Chỉ trừ lượt Cookie VIP đã mua khi tạo thành công." : "Sử dụng lượt đã mua hoặc hạn mức của gói hiện tại.";
  modal(`<div class="confirm-icon">${vip ? "🍪" : "⚡"}</div><h2>${vip ? "Rút Cookie VIP" : "Tạo NFToken"}</h2><p>${usageText}</p><button class="button wide" data-run-nftoken>Bắt đầu xử lý</button>`, {onOpen(root, close) {
    root.querySelector("[data-run-nftoken]").onclick = async (event) => {
      const done = busyButton(event.currentTarget); const quantity = 1;
      let failed = false;
      try {
        const result = await api.nftoken(mode, quantity, requestId); syncQuota(result.quota); close();
        modal(`<div class="confirm-icon">✓</div><h2>Tạo thành công ${result.items.length} NFToken</h2>${result.items.map((item, index) => `<article class="token-result"><b>Tài khoản ${index + 1}</b>${accountSummary(item.account)}<a class="button wide" href="${escapeHtml(item.link)}" target="_blank" rel="noopener">Mở Netflix</a>${item.downloadUrl ? `<a class="button secondary wide" href="${escapeHtml(item.downloadUrl)}">Tải file NFToken bảo mật</a>` : ""}<button class="button secondary wide" data-copy-link="${escapeHtml(item.link)}">Sao chép link</button></article>`).join("")}`, {onOpen(resultRoot) { resultRoot.querySelectorAll("[data-copy-link]").forEach((button) => button.onclick = () => copyText(button.dataset.copyLink)); }});
      } catch (error) {
        failed = true;
        const message = error.reasonCode === "nftoken_timeout"
          ? "Máy chủ xử lý quá lâu, vui lòng thử lại"
          : error.message;
        toast(message, "error");
      } finally {
        done();
        if (failed && event.currentTarget.isConnected) event.currentTarget.textContent = "Thử lại";
      }
    };
  }});
}

export function claimFreeCookie() {
  modal(`<div class="confirm-icon">🎁</div><h2>Cookie miễn phí</h2><p>Cookie được lấy theo đúng hạn mức gói của bạn.</p><button class="button wide" data-claim-free>Nhận Cookie ngay</button>`, {onOpen(root, close) {
    root.querySelector("[data-claim-free]").onclick = async (event) => {
      const done = busyButton(event.currentTarget);
      try { const result = await api.freeCookie(); syncQuota(result.quota); state.bootstrap.checkin = result.checkin || state.bootstrap.checkin; close(); modal(`<div class="confirm-icon">✓</div><h2>Cookie của bạn</h2><p class="privacy-banner">${escapeHtml(result.copyright?.text || "© mnhut - NFToken Pro")}</p><textarea class="result-text" readonly>${escapeHtml(result.cookie || "")}</textarea>${result.downloadUrl ? `<a class="button wide" href="${escapeHtml(result.downloadUrl)}">Tải Cookie bảo mật</a>` : ""}<button class="button secondary wide" data-copy-cookie>Sao chép Cookie</button>`, {onOpen(resultRoot) { resultRoot.querySelector("[data-copy-cookie]").onclick = () => copyText(result.cookie || ""); }}); }
      catch (error) { toast(error.message, "error"); done(); }
    };
  }});
}

function tvLogMarkup(items = []) {
  const fallback = [
    {label: "Kiểm tra mã TV", status: "pending"},
    {label: "Chuẩn bị Cookie Premium live", status: "pending"},
    {label: "Mở phiên Netflix bảo mật", status: "pending"},
    {label: "Gửi mã kết nối tới TV", status: "pending"},
    {label: "Xác nhận kết nối", status: "pending"},
  ];
  const rows = items.length ? items : fallback;
  const symbol = {done: "✓", active: "•", error: "!", pending: ""};
  return `<div class="tv-log" data-tv-log>${rows.map((item) => `<div class="tv-log-row ${escapeHtml(item.status || "pending")}" data-tv-step="${escapeHtml(item.key || "")}"><i>${symbol[item.status] || ""}</i><span>${escapeHtml(item.label)}</span><small>${item.status === "done" ? "Hoàn tất" : item.status === "error" ? "Cần kiểm tra" : item.status === "active" ? "Đang xử lý" : "Chờ xử lý"}</small></div>`).join("")}</div>`;
}

export function openTvLogin() {
  modal(`<div class="tv-hero"><span>📺</span><div><h2>Đăng nhập TV</h2></div></div><label class="field">Mã TV<input id="tv-code" inputmode="numeric" maxlength="9" autocomplete="one-time-code" placeholder="Nhập 8 chữ số"></label>${tvLogMarkup()}<button class="button wide" data-run-tv>Kiểm tra & kết nối</button>`, {onOpen(root) {
    const button = root.querySelector("[data-run-tv]");
    const input = root.querySelector("#tv-code");
    const renderLog = (items) => { const log = root.querySelector("[data-tv-log]"); if (log) log.outerHTML = tvLogMarkup(items); };
    button.onclick = async (event) => {
      const code = input.value.replace(/[\s-]/g, "");
      if (!/^\d{8}$/.test(code)) { input.focus(); toast("Mã TV phải gồm đúng 8 chữ số", "error"); return; }
      const done = busyButton(event.currentTarget, "Đang kết nối..."); input.disabled = true;
      let stage = 0;
      const timer = setInterval(() => {
        stage = Math.min(stage + 1, 3);
        renderLog(["validate", "cookie", "browser", "connect", "done"].map((key, index) => ({key, label: ["Kiểm tra mã TV", "Chuẩn bị Cookie Premium live", "Mở phiên Netflix bảo mật", "Gửi mã kết nối tới TV", "Xác nhận kết nối"][index], status: index < stage ? "done" : index === stage ? "active" : "pending"})));
      }, 1200);
      try {
        const result = await api.tvLogin(code);
        clearInterval(timer); renderLog(result.steps || []);
        setTimeout(() => { const close = modal(`<div class="confirm-icon">✓</div><h2>${escapeHtml(result.message)}</h2>${accountSummary(result.account)}<button class="button wide" data-close-result>Hoàn tất</button>`); document.querySelector("[data-close-result]")?.addEventListener("click", close); }, 280);
      } catch (error) {
        clearInterval(timer); renderLog(error.payload?.steps || [{key: "connect", label: "Kết nối Netflix TV", status: "error"}]);
        const sheet = root.querySelector(".modal-sheet");
        sheet.querySelector("[data-tv-error]")?.remove();
        sheet.insertAdjacentHTML("beforeend", `<div class="tv-error" data-tv-error>⚠️ <b>${escapeHtml(error.reasonCode || "unknown_error")}</b> · ${escapeHtml(error.message)}</div>`);
        input.disabled = false; done();
      }
    };
  }});
}

export function openCheckin() {
  modal(`<div class="confirm-icon">✅</div><h2>Điểm danh Cookie Free</h2><p>Mỗi ngày bạn nhận 2 lượt theo múi giờ Việt Nam. Chỉ trừ lượt sau khi lấy Cookie thành công.</p><button class="button wide" data-checkin>Điểm danh hôm nay</button>`, {onOpen(root, close) {
    root.querySelector("[data-checkin]").onclick = async (event) => { const done = busyButton(event.currentTarget); try { const result = await api.checkin(); state.bootstrap.checkin = result.checkin; close(); toast(result.new ? "Đã nhận 2 lượt Cookie hôm nay" : "Bạn đã điểm danh hôm nay"); update({bootstrap: state.bootstrap}); } catch (error) { toast(error.message, "error"); done(); } };
  }});
}

export function openReferral() {
  const referral = state.bootstrap.referral || {};
  modal(`<div class="confirm-icon">🔗</div><h2>Giới thiệu bạn bè</h2><p>Đã có ${referral.count || 0}/5 người hợp lệ. Đủ 5 người sẽ nhận đúng 2 lượt NFToken.</p><label class="field">Link giới thiệu<input readonly value="${escapeHtml(referral.link || "")}"></label><button class="button wide" data-copy-referral>Sao chép link</button>`, {onOpen(root) { root.querySelector("[data-copy-referral]").onclick = () => copyText(referral.link || ""); }});
}

export async function openNotifications() {
  try {
    const result = await api.notifications();
    modal(`<div class="eyebrow">Shop MMO</div><h2>Thông báo</h2>${result.items.length ? result.items.map((item) => `<article class="notification-item ${item.is_read ? "read" : "unread"}"><b>${escapeHtml(item.title)}</b><p>${escapeHtml(item.body)}</p><small>${escapeHtml(item.created_at)}</small></article>`).join("") : emptyState("Chưa có thông báo", "Thông báo hệ thống sẽ xuất hiện tại đây.")}<button class="button secondary wide" data-read-all>Đánh dấu đã đọc</button>`, {onOpen(root) { root.querySelector("[data-read-all]")?.addEventListener("click", async () => { await api.markNotificationsRead([], true); toast("Đã đánh dấu đã đọc"); }); }});
    if (result.unread) { await api.markNotificationsRead([], true); update({notificationUnread: 0}); }
  } catch (error) { toast(error.message, "error"); }
}

export async function openMissions() {
  try {
    const result = await api.missions();
    modal(`<div class="eyebrow">Shop MMO</div><h2>Nhiệm vụ</h2>${result.items.map((mission) => `<article class="notification-item"><b>${escapeHtml(mission.name)}</b><p>${escapeHtml(mission.description)}</p><small>${mission.claimed ? "Đã nhận" : mission.completed ? "Đủ điều kiện" : "Chưa hoàn thành"}</small>${mission.completed && !mission.claimed ? `<button class="button wide" data-claim-mission="${mission.id}">Nhận ${mission.rewardCredits} lượt NFToken</button>` : ""}</article>`).join("")}`, {onOpen(root, close) { root.querySelectorAll("[data-claim-mission]").forEach((button) => button.onclick = async () => { try { await api.claimMission(button.dataset.claimMission); close(); toast("Đã nhận thưởng nhiệm vụ"); } catch (error) { toast(error.message, "error"); } }); }});
  } catch (error) { toast(error.message, "error"); }
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

const depositLabels = {
  AWAITING_PAYMENT: ["Chờ bạn chuyển khoản", "waiting"],
  PENDING: ["Admin đang kiểm tra", "pending"],
  APPROVED: ["Đã duyệt & cộng tiền", "approved"],
  REJECTED: ["Đã từ chối", "rejected"],
};

function depositRows(items) {
  if (!items.length) return '<div class="deposit-empty">Chưa có yêu cầu nạp tiền nào.</div>';
  return items.map((tx) => {
    const [label, kind] = depositLabels[tx.status] || [tx.status, "waiting"];
    return `<article class="deposit-item ${kind}"><div class="deposit-item-top"><span><b>#${tx.id}</b><small>${escapeHtml(tx.created_at || "Mới tạo")}</small></span><em>${escapeHtml(label)}</em></div><strong>${formatMoney(tx.amount)}</strong>${tx.review_note ? `<p>${tx.status === "REJECTED" ? "Lý do" : "Ghi chú"}: ${escapeHtml(tx.review_note)}</p>` : ""}${tx.status === "AWAITING_PAYMENT" ? `<button class="button secondary wide" data-submit-old-deposit="${tx.id}">Tôi đã chuyển tiền</button>` : ""}</article>`;
  }).join("");
}

async function showDepositPayment(result, closeWallet) {
  closeWallet?.();
  modal(`<div class="deposit-head"><span>💳</span><div><div class="eyebrow">GIAO DỊCH #${result.transactionId}</div><h2>Quét QR để chuyển khoản</h2></div></div><div class="deposit-amount"><small>SỐ TIỀN CẦN CHUYỂN</small><strong>${formatMoney(result.amount)}</strong></div>${result.qrUrl ? `<div class="deposit-qr-wrap"><img class="deposit-qr" src="${escapeHtml(result.qrUrl)}" alt="QR chuyển khoản"><small>Quét bằng ứng dụng ngân hàng</small></div>` : '<div class="privacy-banner">Admin chưa cấu hình QR ngân hàng. Hãy chuyển khoản với nội dung bên dưới.</div>'}<p class="deposit-label">Nội dung chuyển khoản</p><div class="copy-value"><code>${escapeHtml(result.transferNote)}</code><button data-copy-note>${icon("copy")}</button></div><div class="deposit-warning">Chuyển đúng số tiền và nội dung để Admin đối soát nhanh.</div><button class="button wide deposit-paid" data-submit-deposit>✓ Tôi đã chuyển tiền</button><button class="button secondary wide" data-later-deposit>Để sau</button>`, {onOpen(root, close) {
    root.querySelector("[data-copy-note]").onclick = () => copyText(result.transferNote);
    root.querySelector("[data-later-deposit]").onclick = () => { close(); openDeposit(); };
    root.querySelector("[data-submit-deposit]").onclick = async (event) => {
      const done = busyButton(event.currentTarget, "Đang báo Admin...");
      try { await api.submitDeposit(result.transactionId); close(); toast("Đã báo Admin, vui lòng chờ duyệt"); openDeposit(); }
      catch (error) { toast(error.message, "error"); done(); }
    };
  }});
}

export async function openDeposit() {
  try {
    const history = await api.transactions();
    modal(`<div class="wallet-hero"><div><small>VÍ NFTOKEN</small><h2 data-wallet-balance>${formatMoney(state.bootstrap.user.balance)}</h2><p>Số dư khả dụng</p></div><span>${icon("wallet")}</span></div><section class="deposit-create"><h3>Nạp tiền nhanh</h3><div class="deposit-presets"><button data-deposit-value="50000">50K</button><button data-deposit-value="100000">100K</button><button data-deposit-value="200000">200K</button><button data-deposit-value="500000">500K</button></div><label class="field">Hoặc nhập số tiền<input id="deposit-amount" type="number" min="10000" max="100000000" step="10000" value="50000"></label><button class="button wide" data-create-deposit>Tạo mã QR</button></section><div class="deposit-history-title"><h3>Lịch sử nạp tiền</h3><button data-refresh-deposits>↻ Làm mới</button></div><div data-deposit-list>${depositRows(history.items)}</div>`, {onOpen(root, close) {
      root.querySelectorAll("[data-deposit-value]").forEach((button) => button.onclick = () => { root.querySelector("#deposit-amount").value = button.dataset.depositValue; root.querySelectorAll("[data-deposit-value]").forEach((item) => item.classList.toggle("active", item === button)); });
      let knownStatuses = Object.fromEntries(history.items.map((item) => [item.id, item.status]));
      const refresh = async () => { const [result, bootstrap] = await Promise.all([api.transactions(), api.bootstrap()]); const list = root.querySelector("[data-deposit-list]"); if (!list) return; result.items.forEach((item) => { if (knownStatuses[item.id] && knownStatuses[item.id] !== item.status && ["APPROVED","REJECTED"].includes(item.status)) toast(item.status === "APPROVED" ? `Giao dịch #${item.id} đã được duyệt` : `Giao dịch #${item.id} bị từ chối`, item.status === "APPROVED" ? "success" : "error"); }); knownStatuses = Object.fromEntries(result.items.map((item) => [item.id, item.status])); state.bootstrap = bootstrap; root.querySelector("[data-wallet-balance]").textContent = formatMoney(bootstrap.user.balance); list.innerHTML = depositRows(result.items); bindSubmitOld(); };
      const bindSubmitOld = () => root.querySelectorAll("[data-submit-old-deposit]").forEach((button) => button.onclick = async () => { try { await api.submitDeposit(button.dataset.submitOldDeposit); toast("Đã báo Admin"); await refresh(); } catch (error) { toast(error.message, "error"); } });
      bindSubmitOld();
      root.querySelector("[data-refresh-deposits]").onclick = () => refresh().catch((error) => toast(error.message, "error"));
      root.querySelector("[data-create-deposit]").onclick = async (event) => { const done = busyButton(event.currentTarget); try { const result = await api.deposit(Number(root.querySelector("#deposit-amount").value)); showDepositPayment(result, close); } catch (error) { toast(error.message, "error"); done(); } };
      const timer = setInterval(() => { if (!root.querySelector("[data-deposit-list]")) return clearInterval(timer); refresh().catch(() => {}); }, 7000);
    }});
  } catch (error) { toast(error.message, "error"); }
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
  modal(`<div class="confirm-icon">📚</div><h2>Hướng dẫn sử dụng</h2><div class="help-list"><p><b>NFToken theo gói</b><small>Dùng hạn mức hằng ngày của gói thành viên.</small></p><p><b>Cookie VIP</b><small>Dùng lượt đã mua trong cửa hàng; chỉ trừ khi thành công.</small></p><p><b>Đăng nhập TV</b><small>Mã TV có thời hạn ngắn, hãy nhập ngay khi TV hiển thị.</small></p><p><b>Nạp tiền</b><small>Quét QR, bấm “Tôi đã chuyển tiền” và theo dõi kết quả duyệt ngay trong Mini App.</small></p></div>`);
}
