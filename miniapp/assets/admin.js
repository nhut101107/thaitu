import {api} from "./api.js?v=13";
import {state, update} from "./state.js";
import {emptyState, escapeHtml, formatMoney, modal, skeleton, toast} from "./components.js?v=13";

export async function loadAdmin(query = "") {
  try {
    const [admin, reports, flashSales, leaderboards, missions] = await Promise.all([api.adminDashboard(query), api.adminReports(), api.adminFlashSales(), api.adminReferralLeaderboards(), api.adminMissions()]);
    admin.reports = reports;
    admin.flashSales = flashSales.items || [];
    admin.leaderboards = leaderboards.items || [];
    admin.missions = missions.items || [];
    const categories = [...new Set((admin.products || []).map((item) => item.category).filter(Boolean))];
    update({admin, products: admin.products || [], categories});
  } catch (error) {
    toast(error.message, "error");
  }
}

function adminRows(items, renderer, emptyText) {
  return items?.length ? `<div class="admin-list">${items.map(renderer).join("")}</div>` : emptyState("Chưa có dữ liệu", emptyText);
}

function busyButton(button, text) {
  const old = button.textContent;
  button.disabled = true;
  button.textContent = text;
  return () => { button.disabled = false; button.textContent = old; };
}

function productThumb(item) {
  const fallback = `<div class="auto-product-art" ${item.imageUrl ? "hidden" : ""}><span>${escapeHtml((item.name || "N").slice(0, 1).toUpperCase())}</span><b>N</b></div>`;
  const image = item.imageUrl ? `<img data-admin-product-image src="${escapeHtml(item.imageUrl)}" alt="">` : "";
  return `<div class="admin-product-row-art"><div class="product-image">${image}${fallback}</div></div>`;
}

export function adminView() {
  if (!state.bootstrap?.isAdmin) return `<div class="page">${emptyState("Không có quyền", "Khu vực này chỉ dành cho Admin.")}</div>`;
  const data = state.admin;
  if (!data) return `<div class="page admin-page"><div class="eyebrow">MNHUT CONTROL CENTER</div><h1>Trung tâm quản trị</h1>${skeleton(4)}</div>`;
  const s = data.stats;
  const featureLabels = {tv:"Netflix TV",planToken:"NFToken theo gói",vipToken:"Cookie VIP",freeCookie:"Cookie miễn phí",giftcode:"Giftcode",deposit:"Nạp tiền",support:"Hỗ trợ"};
  return `<div class="page admin-page">
    <div class="eyebrow">MNHUT CONTROL CENTER</div><div class="admin-title"><div><h1>Trung tâm quản trị</h1><p>Dữ liệu thật · cập nhật trực tiếp</p></div><button data-admin-reload>↻</button></div>
    <div class="admin-stats">
      <article><i>👥</i><small>Người dùng</small><b>${s.users}</b></article>
      <article><i>💰</i><small>Doanh thu</small><b>${formatMoney(s.revenue)}</b></article>
      <article><i>🧾</i><small>Đơn hàng</small><b>${s.orders}</b></article>
      <article><i>⏳</i><small>Chờ duyệt nạp</small><b>${s.pendingDeposits}</b></article>
      <article><i>🍪</i><small>Kho Premium / Free</small><b>${s.premiumStock} / ${s.freeStock}</b></article>
      <article><i>🛟</i><small>Hỗ trợ mở</small><b>${s.openTickets}</b></article>
    </div>

    <details class="admin-section" open><summary><span><i>⚙️</i><b>Vận hành hệ thống</b><small>Bảo trì, thông báo và bật/tắt chức năng</small></span><em>⌄</em></summary><div class="admin-section-body"><form data-admin-settings><label class="danger-check"><input name="maintenance" type="checkbox" ${data.settings.maintenance ? "checked" : ""}> Bật chế độ bảo trì cho khách</label><label class="field">Thông báo trên trang chủ<textarea name="announcement" maxlength="500" placeholder="Để trống nếu không có thông báo">${escapeHtml(data.settings.announcement || "")}</textarea></label><div class="feature-switches">${Object.entries(featureLabels).map(([key,label]) => `<label><input name="feature_${key}" type="checkbox" ${data.settings.features[key] ? "checked" : ""}><span>${label}</span></label>`).join("")}</div><button class="button wide">Lưu cấu hình hệ thống</button></form></div></details>

    <details class="admin-section"><summary><span><i>📦</i><b>Quản lý kho Cookie</b><small>Premium ${s.premiumStock} · Free ${s.freeStock}</small></span><em>⌄</em></summary><div class="admin-section-body"><div class="inventory-cards"><article><b>Premium</b><small>${s.premiumStock} khả dụng · ${s.premiumUsed} đã dùng</small><button data-admin-inventory="premium">Thêm vào kho</button><button class="clean" data-admin-cleanup="premium">Dọn mục đã dùng</button></article><article><b>Free</b><small>${s.freeStock} khả dụng · ${s.freeUsed} đã dùng</small><button data-admin-inventory="free">Thêm vào kho</button><button class="clean" data-admin-cleanup="free">Dọn mục đã dùng</button></article></div><p class="admin-note">Dữ liệu Cookie chỉ được ghi vào SQLite, không có API đọc ngược nội dung ra giao diện.</p></div></details>

    <details class="admin-section" open><summary><span><i>🛍</i><b>Sản phẩm cửa hàng</b><small>${data.products.length} sản phẩm</small></span><em>⌄</em></summary><div class="admin-section-body"><button class="button admin-add" data-admin-product-new>＋ Thêm sản phẩm</button>${adminRows(data.products, (item) => `<article class="admin-row admin-product-item">${productThumb(item)}<div class="admin-product-info"><b>${escapeHtml(item.name)}</b><small>${formatMoney(item.price)} · ⚡ ${item.nftokenCredits || 0} lượt NFToken · 🍪 ${item.credits} lượt Cookie VIP · ${item.available ? "Đang bán" : "Đã ẩn"}</small></div><span><button data-admin-product="${item.id}">Sửa</button><button class="delete-product" data-admin-product-delete="${item.id}">Xóa</button></span></article>`, "Hãy tạo sản phẩm đầu tiên.")}</div></details>

    <details class="admin-section"><summary><span><i>⚡</i><b>Gói & hạn mức chức năng</b><small>NFToken và Cookie miễn phí mỗi ngày</small></span><em>⌄</em></summary><div class="admin-section-body"><button class="button admin-add" data-admin-plan-new>＋ Tạo gói hạn mức</button>${adminRows(data.plans, (plan) => `<article class="admin-row"><div><b>${escapeHtml(plan.name)}</b><small>${plan.tokens_max} NFToken/ngày · ${plan.cookies_max} Cookie/ngày</small></div><button data-admin-plan="${escapeHtml(plan.name)}">Sửa</button></article>`, "Chưa có gói hạn mức.")}</div></details>

    <details class="admin-section" open><summary><span><i>💸</i><b>Duyệt nạp tiền</b><small>${s.pendingDeposits} khách đã báo chuyển khoản</small></span><em>⌄</em></summary><div class="admin-section-body">${adminRows(data.transactions, (tx) => `<article class="admin-row transaction ${tx.status.toLowerCase()}"><div><b>#${tx.id} · ${formatMoney(tx.amount)}</b><small>ID ${tx.user_id}${tx.username ? ` · @${escapeHtml(tx.username)}` : ""}<br>${tx.transfer_note ? `Nội dung: ${escapeHtml(tx.transfer_note)} · ` : ""}${escapeHtml(tx.submitted_at || tx.created_at || "")}</small>${tx.review_note ? `<small class="review-note">${escapeHtml(tx.review_note)}</small>` : ""}</div>${tx.status === "PENDING" ? `<span><button class="approve" data-admin-tx="${tx.id}" data-status="APPROVED">Duyệt</button><button class="reject" data-admin-tx="${tx.id}" data-status="REJECTED">Từ chối</button></span>` : `<em class="tx-state">${tx.status === "AWAITING_PAYMENT" ? "Chưa báo chuyển" : escapeHtml(tx.status)}</em>`}</article>`, "Chưa có giao dịch nạp.")}</div></details>

    <details class="admin-section"><summary><span><i>🧾</i><b>Quản lý đơn hàng</b><small>${data.orders.length} đơn gần nhất</small></span><em>⌄</em></summary><div class="admin-section-body">${adminRows(data.orders, (order) => `<article class="admin-row"><div><b>#${order.id} · ${escapeHtml(order.plan_name)}</b><small>ID ${order.user_id} · ${formatMoney(order.price)} · ${escapeHtml(order.status || "COMPLETED")} · ${escapeHtml(order.date)}</small></div><button data-admin-order="${order.id}">Sửa</button></article>`, "Chưa có đơn hàng.")}</div></details>

    <details class="admin-section"><summary><span><i>👤</i><b>Quản lý người dùng</b><small>Số dư, lượt, gói và khóa tài khoản</small></span><em>⌄</em></summary><div class="admin-section-body"><form class="admin-search" data-admin-search><input name="q" placeholder="Nhập Telegram ID hoặc username"><button>Tìm</button></form>${adminRows(data.users, (user) => `<article class="admin-row"><div><b>${user.username ? `@${escapeHtml(user.username)}` : "Không username"}</b><small>ID ${user.user_id} · ${formatMoney(user.balance)} · ⚡ ${user.nftoken_credits || 0} NFToken · 🍪 ${user.credits} VIP · ${escapeHtml(user.plan_name)}${user.is_banned ? " · ĐÃ KHÓA" : ""}</small></div><button data-admin-user="${user.user_id}">Sửa</button></article>`, "Không tìm thấy người dùng.")}</div></details>

    <details class="admin-section"><summary><span><i>🎟</i><b>Mã quà tặng</b><small>Tạo và cập nhật giftcode</small></span><em>⌄</em></summary><div class="admin-section-body"><button class="button admin-add" data-admin-code-new>＋ Tạo giftcode</button>${adminRows(data.codes, (code) => `<article class="admin-row"><div><b>${escapeHtml(code.code)}</b><small>${formatMoney(code.amount)} · còn ${code.uses} lượt</small></div><button data-admin-code="${escapeHtml(code.code)}">Sửa</button></article>`, "Chưa có giftcode.")}</div></details>

    <details class="admin-section"><summary><span><i>🛟</i><b>Yêu cầu hỗ trợ</b><small>${s.openTickets} yêu cầu chưa đóng</small></span><em>⌄</em></summary><div class="admin-section-body">${adminRows(data.tickets, (ticket) => `<article class="admin-ticket"><header><b>#${ticket.id} · ID ${ticket.user_id}</b><em>${escapeHtml(ticket.status)}</em></header><p>${escapeHtml(ticket.message)}</p><small>${escapeHtml(ticket.created_at)}</small><div><button data-admin-reply="${ticket.id}">Trả lời</button><button data-admin-ticket="${ticket.id}" data-status="${ticket.status === "OPEN" ? "CLOSED" : "OPEN"}">${ticket.status === "OPEN" ? "Đã xử lý" : "Mở lại"}</button></div></article>`, "Chưa có yêu cầu hỗ trợ.")}</div></details>

    <details class="admin-section"><summary><span><i>🧭</i><b>Nhật ký quản trị</b><small>100 thao tác gần nhất</small></span><em>⌄</em></summary><div class="admin-section-body">${adminRows(data.audit, (entry) => `<article class="audit-row"><b>${escapeHtml(entry.action)}</b><span>${escapeHtml(entry.target)}</span><p>${escapeHtml(entry.details || "Không có chi tiết")}</p><small>${escapeHtml(entry.created_at)} · Admin ${entry.admin_id}</small></article>`, "Chưa có thao tác quản trị.")}</div></details>
    <details class="admin-section" open><summary><span><i>API</i><b>Product Providers</b><small>${(data.providers || []).length} nguồn API, API key không hiển thị</small></span><em>⌄</em></summary><div class="admin-section-body"><button class="button admin-add" data-admin-provider-new>+ Thêm provider</button>${adminRows(data.providers, (provider) => `<article class="admin-row"><div><b>${escapeHtml(provider.name)}</b><small>${escapeHtml(provider.baseUrl)} · timeout ${provider.timeout}s · ${provider.enabled ? "Đang bật" : "Đã tắt"}</small></div><span><button data-admin-provider-test="${provider.id}">Test</button><button data-admin-provider-sync="${provider.id}">Sync</button><button data-admin-provider-toggle="${provider.id}" data-enabled="${provider.enabled ? "0" : "1"}">${provider.enabled ? "Tắt" : "Bật"}</button></span></article>`, "Chưa có provider.")}</div></details>
    <details class="admin-section"><summary><span><i>★</i><b>Hạng khách hàng</b><small>Điều chỉnh mốc referral và chi tiêu</small></span><em>⌄</em></summary><div class="admin-section-body"><button class="button" data-admin-ranks>Chỉnh mốc hạng</button></div></details>
    <details class="admin-section"><summary><span><i>🏆</i><b>Bảng xếp hạng referral</b><small>Tuần / tháng, tự chốt chu kỳ và thưởng</small></span><em>⌄</em></summary><div class="admin-section-body"><button class="button" data-admin-leaderboards>Cấu hình reward</button>${(data.leaderboards || []).map((board) => `<article class="admin-row"><div><b>${board.period}</b><small>Top ${board.items.length} · ${board.settings.rewardEnabled ? `thưởng ${board.settings.rewardCredits}` : "đang tắt thưởng"}</small></div></article>`).join("")}</div></details>
    <details class="admin-section"><summary><span><i>©</i><b>Thương hiệu và bản quyền</b><small>Watermark file export và ảnh thương hiệu</small></span><em>⌄</em></summary><div class="admin-section-body"><button class="button" data-admin-copyright>Cấu hình bản quyền / ảnh</button></div></details>
    <details class="admin-section"><summary><span><i>🎯</i><b>Nhiệm vụ</b><small>${(data.missions || []).length} nhiệm vụ</small></span><em>⌄</em></summary><div class="admin-section-body"><button class="button admin-add" data-admin-mission-new>+ Tạo nhiệm vụ</button>${adminRows(data.missions, (mission) => `<article class="admin-row"><div><b>${escapeHtml(mission.name)}</b><small>${escapeHtml(mission.code)} · thưởng ${mission.reward_credits} · ${mission.active ? "Bật" : "Tắt"}</small></div></article>`, "Chưa có nhiệm vụ.")}</div></details>
    <details class="admin-section"><summary><span><i>🔔</i><b>Trung tâm thông báo</b><small>Gửi thông báo hệ thống hoặc theo user</small></span><em>⌄</em></summary><div class="admin-section-body"><button class="button" data-admin-notification-new>Gửi thông báo</button></div></details>
    <details class="admin-section" open><summary><span><i>▣</i><b>Reports doanh thu</b><small>Database thật, lọc và xuất CSV UTF-8</small></span><em>⌄</em></summary><div class="admin-section-body"><div class="report-summary"><b>${formatMoney(data.reports?.summary?.revenue || 0)}</b><span>${data.reports?.summary?.orders || 0} đơn · giảm ${formatMoney(data.reports?.summary?.discounts || 0)}</span></div><button class="button wide" data-admin-report-csv>Tải CSV báo cáo</button></div></details>
    <details class="admin-section"><summary><span><i>⚡</i><b>Flash Sale</b><small>${(data.flashSales || []).length} chương trình</small></span><em>⌄</em></summary><div class="admin-section-body"><button class="button admin-add" data-admin-flash-new>+ Tạo Flash Sale</button>${adminRows(data.flashSales, (sale) => `<article class="admin-row"><div><b>${escapeHtml(sale.name)}</b><small>${escapeHtml(sale.product_name)} · -${sale.discount_percent}% · còn ${Math.max(0, sale.quantity_limit - sale.quantity_sold)}</small></div><button data-admin-flash-toggle="${sale.id}" data-enabled="${sale.active ? "0" : "1"}">${sale.active ? "Tắt" : "Bật"}</button></article>`, "Chưa có Flash Sale.")}</div></details>
  </div>`;
}

function providerDialog(provider = null) {
  modal(`<div class="eyebrow">PRODUCT PROVIDER</div><h2>${provider ? "Sửa provider" : "Thêm provider"}</h2><form data-provider-form><label class="field">Tên<input name="name" required value="${escapeHtml(provider?.name || "")}"></label><label class="field">Base URL<input name="baseUrl" type="url" required value="${escapeHtml(provider?.baseUrl || "")}"></label><label class="field">API key<input name="apiKey" type="password" autocomplete="new-password" placeholder="Để trống để giữ nguyên"></label><label class="field">Ưu tiên fallback<input name="priority" type="number" min="1" max="10000" value="${provider?.priority || 100}"></label><label class="field">Timeout (giây)<input name="timeout" type="number" min="1" max="60" value="${provider?.timeout || 10}"></label><label><input name="enabled" type="checkbox" ${provider?.enabled !== false ? "checked" : ""}> Bật provider</label><button class="button wide">Lưu provider</button></form>`, {onOpen(root, close) { root.querySelector("[data-provider-form]").onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); const value = {name: form.get("name"), baseUrl: form.get("baseUrl"), timeout: Number(form.get("timeout")), priority: Number(form.get("priority")), enabled: form.has("enabled")}; const key = form.get("apiKey"); if (key) value.apiKey = key; try { provider ? await api.adminUpdateProvider(provider.id, value) : await api.adminCreateProvider({...value, apiKey: key || ""}); close(); await loadAdmin(); toast("Đã lưu provider"); } catch (error) { toast(error.message, "error"); } }; }});
}

function rankDialog() {
  const ranks = state.admin.ranks || [];
  modal(`<div class="eyebrow">CUSTOMER RANKS</div><h2>Mốc lên hạng</h2><form data-rank-form>${ranks.map((rank) => `<label class="field">${escapeHtml(rank.rank)} · referral<input name="ref_${escapeHtml(rank.rank)}" type="number" min="0" value="${rank.referral_threshold}"></label><label class="field">Chi tiêu ${escapeHtml(rank.rank)}<input name="spend_${escapeHtml(rank.rank)}" type="number" min="0" value="${rank.spend_threshold}"></label>`).join("")}<button class="button wide">Lưu mốc hạng</button></form>`, {onOpen(root, close) { root.querySelector("[data-rank-form]").onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { for (const rank of ranks) await api.adminUpdateRank(rank.rank, {referralThreshold: Number(form.get(`ref_${rank.rank}`)), spendThreshold: Number(form.get(`spend_${rank.rank}`)), benefits: rank.benefits}); close(); await loadAdmin(); toast("Đã lưu mốc hạng"); } catch (error) { toast(error.message, "error"); } }; }});
}

function flashSaleDialog() {
  const products = state.admin.products || [];
  modal(`<div class="eyebrow">FLASH SALE</div><h2>Tạo chương trình</h2><form data-flash-form><label class="field">Tên chương trình<input name="name" required maxlength="100"></label><label class="field">Sản phẩm<select name="productId">${products.map((item) => `<option value="${item.id}">${escapeHtml(item.name)}</option>`).join("")}</select></label><div class="field-pair"><label class="field">Giảm %<input name="discountPercent" type="number" min="1" max="99" value="10"></label><label class="field">Số lượng<input name="quantityLimit" type="number" min="1" value="10"></label></div><div class="field-pair"><label class="field">Bắt đầu<input name="startsAt" type="datetime-local" required></label><label class="field">Kết thúc<input name="endsAt" type="datetime-local" required></label></div><label><input name="allowPromo" type="checkbox"> Cho phép cộng mã giảm giá</label><button class="button wide">Tạo Flash Sale</button></form>`, {onOpen(root, close) { root.querySelector("[data-flash-form]").onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { await api.adminCreateFlashSale({name: form.get("name"), productId: Number(form.get("productId")), discountPercent: Number(form.get("discountPercent")), quantityLimit: Number(form.get("quantityLimit")), startsAt: form.get("startsAt"), endsAt: form.get("endsAt"), allowPromo: form.has("allowPromo")}); close(); await loadAdmin(); toast("Đã tạo Flash Sale"); } catch (error) { toast(error.message, "error"); } }; }});
}

function leaderboardDialog() {
  const boards = state.admin.leaderboards || [];
  modal(`<div class="eyebrow">REFERRAL LEADERBOARD</div><h2>Cấu hình thưởng</h2><form data-leaderboard-form>${boards.map((board) => `<fieldset><legend>${board.period}</legend><label><input name="enabled_${board.period}" type="checkbox" ${board.settings.rewardEnabled ? "checked" : ""}> Bật thưởng</label><label class="field">Lượt NFToken<input name="credits_${board.period}" type="number" min="0" value="${board.settings.rewardCredits}"></label><label class="field">Số người nhận<input name="limit_${board.period}" type="number" min="1" max="20" value="${board.settings.winnerLimit}"></label></fieldset>`).join("")}<button class="button wide">Lưu cấu hình</button></form>`, {onOpen(root, close) { root.querySelector("[data-leaderboard-form]").onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { for (const board of boards) await api.adminUpdateReferralLeaderboard(board.period, {rewardEnabled: form.has(`enabled_${board.period}`), rewardCredits: Number(form.get(`credits_${board.period}`)), winnerLimit: Number(form.get(`limit_${board.period}`))}); close(); await loadAdmin(); toast("Đã lưu cấu hình bảng xếp hạng"); } catch (error) { toast(error.message, "error"); } }; }});
}

function missionDialog() {
  modal(`<div class="eyebrow">MISSIONS</div><h2>Tạo nhiệm vụ</h2><form data-mission-form><label class="field">Mã nhiệm vụ<input name="code" maxlength="50" required></label><label class="field">Tên<input name="name" maxlength="150" required></label><label class="field">Mô tả<textarea name="description" maxlength="500"></textarea></label><label class="field">Thưởng NFToken<input name="rewardCredits" type="number" min="0" value="1"></label><label class="field">Điều kiện<select name="type"><option value="checkin">Điểm danh</option><option value="first_order">Đơn đầu tiên</option><option value="referral">Referral</option><option value="promotion">Dùng mã giảm giá</option></select></label><button class="button wide">Tạo nhiệm vụ</button></form>`, {onOpen(root, close) { root.querySelector("[data-mission-form]").onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { await api.adminCreateMission({code: form.get("code"), name: form.get("name"), description: form.get("description"), rewardCredits: Number(form.get("rewardCredits")), condition: {type: form.get("type")}}); close(); await loadAdmin(); toast("Đã tạo nhiệm vụ"); } catch (error) { toast(error.message, "error"); } }; }});
}

function notificationDialog() {
  modal(`<div class="eyebrow">NOTIFICATIONS</div><h2>Gửi thông báo</h2><form data-notification-form><label class="field">Telegram ID (để trống = tất cả)<input name="userId" inputmode="numeric"></label><label class="field">Tiêu đề<input name="title" maxlength="200" required></label><label class="field">Nội dung<textarea name="body" maxlength="2000" required></textarea></label><button class="button wide">Gửi thông báo</button></form>`, {onOpen(root, close) { root.querySelector("[data-notification-form]").onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { await api.adminCreateNotification({userId: form.get("userId"), title: form.get("title"), body: form.get("body")}); close(); toast("Đã gửi thông báo"); } catch (error) { toast(error.message, "error"); } }; }});
}

function copyrightDialog() {
  const current = state.admin.copyright || {enabled: true, text: "© mnhut - NFToken Pro"};
  modal(`<div class="eyebrow">BRAND & COPYRIGHT</div><h2>Bản quyền export</h2><form data-copyright-form><label class="field">Nội dung<textarea name="text" maxlength="2000">${escapeHtml(current.text || "")}</textarea></label><label><input name="enabled" type="checkbox" ${current.enabled ? "checked" : ""}> Bật watermark</label><label class="field">Ảnh thương hiệu<input name="file" type="file" accept="image/png,image/jpeg,image/webp"></label><button class="button wide">Lưu cấu hình</button></form>`, {onOpen(root, close) { root.querySelector("[data-copyright-form]").onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { await api.adminUpdateCopyright({enabled: form.has("enabled"), text: form.get("text")}); const file = form.get("file"); if (file?.size) await api.adminUploadBrand(file); close(); await loadAdmin(); toast("Đã lưu bản quyền"); } catch (error) { toast(error.message, "error"); } }; }});
}

function productDialog(item = null) {
  modal(`<div class="eyebrow">QUẢN LÝ CỬA HÀNG</div><h2>${item ? "Sửa sản phẩm" : "Thêm sản phẩm"}</h2><form data-product-form>
    <label class="field">Tên sản phẩm<input name="name" maxlength="80" value="${escapeHtml(item?.name || "")}" required></label>
    <label class="field">Giá bán<input name="price" type="number" min="0" value="${item?.price || 0}" required></label>
    <div class="field-pair"><label class="field">⚡ Lượt tạo link NFToken<input name="nftokenCredits" type="number" min="0" value="${item?.nftokenCredits || 0}" required></label><label class="field">🍪 Lượt lấy Cookie VIP<input name="credits" type="number" min="0" value="${item?.credits || 0}" required></label></div>
    <label class="field">Danh mục<input name="category" maxlength="80" value="${escapeHtml(item?.category || "Gói Cookie VIP")}" required></label>
    <label class="field">Mô tả<textarea name="description" maxlength="1000">${escapeHtml(item?.description || "")}</textarea></label>
    <label class="field">Link ảnh HTTPS<input name="imageUrl" type="url" value="${escapeHtml(item?.imageUrl || "")}" placeholder="https://..."></label>
    <label class="field">Số ngày bảo hành<input name="warrantyDays" type="number" min="0" max="3650" value="${item?.warrantyDays || 0}"></label><label class="check-row"><input name="noWarranty" type="checkbox" ${item && !item.warrantyDays ? "checked" : ""}> Không bảo hành</label>
    <div class="check-row"><label><input name="featured" type="checkbox" ${item?.featured ? "checked" : ""}> Sản phẩm nổi bật</label><label><input name="active" type="checkbox" ${item?.available !== false ? "checked" : ""}> Đang bán</label></div>
    <button class="button wide">${item ? "Lưu thay đổi" : "Tạo sản phẩm"}</button></form>`, {onOpen(root, close) {
      root.querySelector("[data-product-form]").onsubmit = async (event) => {
        event.preventDefault(); const form = new FormData(event.currentTarget);
        const value = {name: form.get("name"), price: Number(form.get("price")), nftokenCredits: Number(form.get("nftokenCredits")), credits: Number(form.get("credits")), category: form.get("category"), description: form.get("description"), imageUrl: form.get("imageUrl"), warrantyDays: form.has("noWarranty") ? 0 : Number(form.get("warrantyDays")), providerId: form.get("providerId") ? Number(form.get("providerId")) : null, externalProductId: form.get("externalProductId"), featured: form.has("featured"), active: form.has("active")};
        try { item ? await api.adminUpdateProduct(item.id, value) : await api.adminCreateProduct(value); close(); await loadAdmin(); toast("Đã lưu sản phẩm"); } catch (error) { toast(`${error.message}${error.reasonCode && error.reasonCode !== "unknown_error" ? ` [${error.reasonCode}]` : ""}`, "error"); }
      };
    }});
}

function planDialog(plan = null) {
  modal(`<div class="eyebrow">HẠN MỨC CHỨC NĂNG</div><h2>${plan ? `Sửa gói ${escapeHtml(plan.name)}` : "Tạo gói mới"}</h2><form data-plan-form><label class="field">Tên gói<input name="name" maxlength="50" value="${escapeHtml(plan?.name || "")}" ${plan ? "readonly" : ""} required></label><div class="field-pair"><label class="field">NFToken/ngày<input name="tokens" type="number" min="0" value="${plan?.tokens_max || 0}" required></label><label class="field">Cookie Free/ngày<input name="cookies" type="number" min="0" value="${plan?.cookies_max || 0}" required></label></div><button class="button wide">Lưu gói hạn mức</button></form>`, {onOpen(root, close) { root.querySelector("[data-plan-form]").onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { await api.adminSavePlan(form.get("name"), {tokensMax: Number(form.get("tokens")), cookiesMax: Number(form.get("cookies"))}); close(); await loadAdmin(); toast("Đã lưu gói"); } catch (error) { toast(error.message, "error"); } }; }});
}

function userDialog(user) {
  const options = state.admin.plans.map((plan) => `<option value="${escapeHtml(plan.name)}" ${plan.name === user.plan_name ? "selected" : ""}>${escapeHtml(plan.name)}</option>`).join("");
  modal(`<div class="eyebrow">USER ID ${user.user_id}</div><h2>${user.username ? `@${escapeHtml(user.username)}` : "Người dùng"}</h2><form data-user-form><label class="field">Số dư<input name="balance" type="number" min="0" value="${user.balance}" required></label><div class="field-pair"><label class="field">Lượt tạo NFToken<input name="nftokenCredits" type="number" min="0" value="${user.nftoken_credits || 0}" required></label><label class="field">Lượt Cookie VIP<input name="credits" type="number" min="0" value="${user.credits}" required></label></div><label class="field">Gói hạn mức<select name="plan">${options}</select></label><label class="danger-check"><input name="banned" type="checkbox" ${user.is_banned ? "checked" : ""}> Khóa tài khoản này</label><button class="button wide">Lưu tài khoản</button></form>`, {onOpen(root, close) { root.querySelector("[data-user-form]").onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { await api.adminUpdateUser(user.user_id, {balance: Number(form.get("balance")), nftokenCredits: Number(form.get("nftokenCredits")), credits: Number(form.get("credits")), plan: form.get("plan"), isBanned: form.has("banned")}); close(); await loadAdmin(); toast("Đã cập nhật người dùng"); } catch (error) { toast(error.message, "error"); } }; }});
}

function codeDialog(code = null) {
  modal(`<div class="eyebrow">GIFT CODE</div><h2>${code ? "Sửa mã quà tặng" : "Tạo mã quà tặng"}</h2><form data-code-form><label class="field">Tên mã<input name="code" maxlength="50" value="${escapeHtml(code?.code || "")}" ${code ? "readonly" : ""} required></label><div class="field-pair"><label class="field">Số tiền cộng<input name="amount" type="number" min="0" value="${code?.amount || 0}" required></label><label class="field">Lượt sử dụng<input name="uses" type="number" min="0" value="${code?.uses || 0}" required></label></div><button class="button wide">Lưu giftcode</button></form>`, {onOpen(root, close) { root.querySelector("[data-code-form]").onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { await api.adminSaveCode(form.get("code"), {amount: Number(form.get("amount")), uses: Number(form.get("uses"))}); close(); await loadAdmin(); toast("Đã lưu giftcode"); } catch (error) { toast(error.message, "error"); } }; }});
}

function transactionDialog(transaction, status) {
  const approve = status === "APPROVED";
  modal(`<div class="confirm-icon">${approve ? "✓" : "!"}</div><div class="eyebrow">GIAO DỊCH #${transaction.id}</div><h2>${approve ? "Duyệt nạp tiền" : "Từ chối giao dịch"}</h2><div class="admin-tx-summary"><span>Khách hàng<b>ID ${transaction.user_id}${transaction.username ? ` · @${escapeHtml(transaction.username)}` : ""}</b></span><span>Số tiền<b>${formatMoney(transaction.amount)}</b></span><span>Nội dung CK<b>${escapeHtml(transaction.transfer_note || "Không có")}</b></span></div><form data-tx-review><label class="field">${approve ? "Ghi chú cho khách (không bắt buộc)" : "Lý do từ chối"}<textarea name="note" maxlength="500" ${approve ? "" : "required minlength=\"3\""} placeholder="${approve ? "Ví dụ: Đã nhận đủ tiền" : "Ví dụ: Chưa nhận được giao dịch ngân hàng"}"></textarea></label><button class="button wide ${approve ? "" : "danger-button"}">${approve ? "Xác nhận cộng tiền" : "Xác nhận từ chối"}</button></form>`, {onOpen(root, close) { root.querySelector("[data-tx-review]").onsubmit = async (event) => { event.preventDefault(); const note = new FormData(event.currentTarget).get("note"); try { await api.adminUpdateTransaction(transaction.id, status, note); close(); await loadAdmin(); toast(approve ? "Đã duyệt và cộng tiền cho khách" : "Đã gửi lý do từ chối trên app"); } catch (error) { toast(error.message, "error"); } }; }});
}

function replyDialog(ticket) {
  modal(`<div class="eyebrow">HỖ TRỢ #${ticket.id}</div><h2>Trả lời người dùng</h2><p>${escapeHtml(ticket.message)}</p><form data-reply-form><label class="field">Nội dung phản hồi<textarea name="message" maxlength="1500" required placeholder="Nhập phản hồi gửi qua Telegram..."></textarea></label><button class="button wide">Gửi phản hồi</button></form>`, {onOpen(root, close) { root.querySelector("[data-reply-form]").onsubmit = async (event) => { event.preventDefault(); const message = new FormData(event.currentTarget).get("message"); try { await api.adminReplySupport(ticket.id, message); close(); await loadAdmin(); toast("Đã gửi phản hồi qua Telegram"); } catch (error) { toast(error.message, "error"); } }; }});
}

function inventoryDialog(kind) {
  const label = kind === "premium" ? "Premium" : "Free";
  modal(`<div class="eyebrow">KHO ${label.toUpperCase()}</div><h2>Nhập và lọc Cookie live</h2><div class="upload-zone"><span>📁</span><b data-file-summary>Chưa chọn file</b><small>Chọn nhiều TXT hoặc nguyên thư mục. ZIP/RAR cũng được hỗ trợ; file không phải Cookie sẽ tự bỏ qua.</small><div class="upload-actions"><label class="button secondary">Chọn nhiều file<input type="file" multiple accept=".txt,.zip,.rar,text/plain,application/zip,application/x-rar-compressed" data-cookie-files></label><label class="button secondary">Chọn thư mục<input type="file" multiple webkitdirectory directory accept=".txt,.zip,.rar,text/plain,application/zip,application/x-rar-compressed" data-cookie-folder></label></div><small data-file-detail>Tối đa theo cấu hình VPS; xử lý nền, không làm treo app.</small></div><button class="button wide" data-upload-cookies>Kiểm tra live & lưu vào kho</button><div class="upload-progress hidden" data-upload-progress><i></i><b data-progress-text>Đang kiểm tra Cookie...</b><small data-progress-detail>Xử lý nền – bạn có thể đóng hộp thoại, user khác vẫn dùng bình thường.</small></div><details class="manual-cookie"><summary>Hoặc nhập thủ công</summary><form data-inventory-form><label class="field">Dữ liệu<textarea name="data" maxlength="60000" required placeholder="NetflixId=...\n---\nNetflixId=..."></textarea></label><button class="button wide">Thêm không kiểm tra live</button></form></details>`, {onOpen(root, close) {
    const fileInputs = [root.querySelector("[data-cookie-files]"), root.querySelector("[data-cookie-folder]")];
    let selectedFiles = [];
    const supported = /\.(txt|zip|rar)$/i;
    const refreshFiles = () => {
      const unique = new Map();
      selectedFiles.forEach((file) => unique.set(`${file.webkitRelativePath || file.name}:${file.size}:${file.lastModified}`, file));
      selectedFiles = [...unique.values()];
      const usable = selectedFiles.filter((file) => supported.test(file.name));
      root.querySelector("[data-file-summary]").textContent = usable.length ? `Đã chọn ${usable.length} file Cookie` : "Chưa chọn file TXT/ZIP/RAR";
      root.querySelector("[data-file-detail]").textContent = selectedFiles.length > usable.length ? `Bỏ qua ${selectedFiles.length - usable.length} file không hỗ trợ` : "Có thể chọn lại để bổ sung file hoặc thư mục";
    };
    fileInputs.forEach((input) => input.addEventListener("change", () => {
      selectedFiles = selectedFiles.concat(Array.from(input.files || []));
      refreshFiles();
    }));
    root.querySelector("[data-upload-cookies]").onclick = async (event) => {
      const files = selectedFiles.filter((file) => supported.test(file.name));
      if (!files.length) return toast("Hãy chọn file TXT/ZIP/RAR hoặc cả thư mục", "error");
      const done = busyButton(event.currentTarget, "Đang tải lên...");
      root.querySelector("[data-upload-progress]").classList.remove("hidden");
      try {
        const result = await api.adminUploadInventory(kind, files);
        if (result.job_id) {
          const progressText = root.querySelector("[data-progress-text]");
          const progressDetail = root.querySelector("[data-progress-detail]");
          const pollJob = async () => {
            try {
              const job = await api.request(`/api/admin/inventory/job/${result.job_id}`);
              if (job.progress) {
                progressText.textContent = `Đang kiểm tra... ${job.progress.checked}/${job.progress.total} (${job.progress.percent}%)`;
                progressDetail.textContent = `✅ Live: ${job.progress.live} · ❌ Lỗi: ${job.progress.dead}`;
              }
              if (job.status === "done") {
                const r = job.result;
                close(); await loadAdmin();
                toast(`Hoàn tất: thêm ${r.added} live, ${r.dead} lỗi, ${r.duplicates} trùng`);
              } else if (job.status === "error") {
                toast(job.result?.error || "Lỗi xử lý", "error");
                root.querySelector("[data-upload-progress]").classList.add("hidden"); done();
              } else {
                setTimeout(pollJob, 2000);
              }
            } catch { setTimeout(pollJob, 3000); }
          };
          progressText.textContent = `Đang kiểm tra ${result.total} Cookie từ ${result.files || files.length} file ở nền...`;
          progressDetail.textContent = result.skipped ? `Bỏ qua ${result.skipped} file không hỗ trợ · user khác vẫn dùng bình thường.` : "Xử lý nền – user khác vẫn dùng bình thường.";
          setTimeout(pollJob, 2000);
        } else {
          close(); await loadAdmin();
          toast(`Đã kiểm tra ${result.checked}: thêm ${result.added} live, ${result.dead} lỗi, ${result.duplicates} trùng`);
        }
      } catch (error) { toast(error.message, "error"); root.querySelector("[data-upload-progress]").classList.add("hidden"); done(); }
    };
    root.querySelector("[data-inventory-form]").onsubmit = async (event) => { event.preventDefault(); const data = new FormData(event.currentTarget).get("data"); try { const result = await api.adminAddInventory(kind, data); close(); await loadAdmin(); toast(`Đã thêm ${result.added} mục, bỏ qua ${result.duplicates} mục trùng`); } catch (error) { toast(error.message, "error"); } };
  }});
}

function orderDialog(order) {
  const dateValue = order.warranty_until ? String(order.warranty_until).slice(0, 10) : "";
  modal(`<div class="eyebrow">ĐƠN HÀNG #${order.id}</div><h2>${escapeHtml(order.plan_name)}</h2><form data-order-form><label class="field">Trạng thái<select name="status">${["PROCESSING","COMPLETED","WARRANTY","CANCELLED"].map((status) => `<option value="${status}" ${status === (order.status || "COMPLETED") ? "selected" : ""}>${status}</option>`).join("")}</select></label><label class="field">Hạn bảo hành<input name="warranty" type="date" value="${escapeHtml(dateValue)}"></label><button class="button wide">Lưu đơn hàng</button></form>`, {onOpen(root, close) { root.querySelector("[data-order-form]").onsubmit = async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); try { await api.adminUpdateOrder(order.id, {status: form.get("status"), warrantyUntil: form.get("warranty")}); close(); await loadAdmin(); toast("Đã cập nhật đơn hàng"); } catch (error) { toast(error.message, "error"); } }; }});
}

export function bindAdminEvents() {
  if (state.route !== "admin" || !state.admin) return;
  document.querySelector("[data-admin-reload]")?.addEventListener("click", () => loadAdmin());
  document.querySelector("[data-admin-report-csv]")?.addEventListener("click", async (event) => { const restore = busyButton(event.currentTarget, "Đang xuất..."); try { const blob = await api.adminReportsCsv(); const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = "nftoken-reports.csv"; link.click(); URL.revokeObjectURL(url); } catch (error) { toast(error.message, "error"); } finally { restore(); } });
  document.querySelector("[data-admin-provider-new]")?.addEventListener("click", () => providerDialog());
  document.querySelector("[data-admin-ranks]")?.addEventListener("click", () => rankDialog());
  document.querySelector("[data-admin-flash-new]")?.addEventListener("click", () => flashSaleDialog());
  document.querySelector("[data-admin-leaderboards]")?.addEventListener("click", () => leaderboardDialog());
  document.querySelector("[data-admin-mission-new]")?.addEventListener("click", () => missionDialog());
  document.querySelector("[data-admin-notification-new]")?.addEventListener("click", () => notificationDialog());
  document.querySelector("[data-admin-copyright]")?.addEventListener("click", async () => { try { const current = await api.adminCopyright(); state.admin.copyright = current; copyrightDialog(); } catch (error) { toast(error.message, "error"); } });
  document.querySelectorAll("[data-admin-provider-test]").forEach((button) => button.onclick = async () => { try { await api.adminTestProvider(button.dataset.adminProviderTest); toast("Provider phản hồi OK"); } catch (error) { toast(error.message, "error"); } });
  document.querySelectorAll("[data-admin-provider-sync]").forEach((button) => button.onclick = async () => { try { const result = await api.adminSyncProvider(button.dataset.adminProviderSync); await loadAdmin(); toast(`Đã đồng bộ ${result.synced} sản phẩm`); } catch (error) { toast(error.message, "error"); } });
  document.querySelectorAll("[data-admin-provider-toggle]").forEach((button) => button.onclick = async () => { const provider = state.admin.providers.find((item) => item.id === Number(button.dataset.adminProviderToggle)); if (!provider) return; try { await api.adminUpdateProvider(provider.id, {name: provider.name, baseUrl: provider.baseUrl, timeout: provider.timeout, priority: provider.priority, enabled: button.dataset.enabled === "1"}); await loadAdmin(); toast("Đã cập nhật trạng thái provider"); } catch (error) { toast(error.message, "error"); } });
  document.querySelectorAll("[data-admin-flash-toggle]").forEach((button) => button.onclick = async () => { try { await api.adminUpdateFlashSale(button.dataset.adminFlashToggle, {active: button.dataset.enabled === "1"}); await loadAdmin(); toast("Đã cập nhật Flash Sale"); } catch (error) { toast(error.message, "error"); } });
  document.querySelector("[data-admin-product-new]")?.addEventListener("click", () => productDialog());
  document.querySelectorAll("[data-admin-product]").forEach((button) => button.onclick = () => productDialog(state.admin.products.find((item) => item.id === Number(button.dataset.adminProduct))));
  document.querySelectorAll("[data-admin-product-image]").forEach((image) => image.addEventListener("error", () => { image.hidden = true; image.nextElementSibling.hidden = false; }));
  document.querySelectorAll("[data-admin-product-delete]").forEach((button) => button.onclick = async () => {
    const item = state.admin.products.find((product) => product.id === Number(button.dataset.adminProductDelete));
    if (!item || !confirm(`Xóa sản phẩm “${item.name}”? Sản phẩm đã có đơn sẽ được ẩn để giữ lịch sử.`)) return;
    const restore = busyButton(button, "...");
    try { const result = await api.adminDeleteProduct(item.id); await loadAdmin(); toast(result.archived ? result.message : "Đã xóa sản phẩm"); }
    catch (error) { toast(`${error.message}${error.reasonCode && error.reasonCode !== "unknown_error" ? ` [${error.reasonCode}]` : ""}`, "error"); }
    finally { restore(); }
  });
  document.querySelector("[data-admin-plan-new]")?.addEventListener("click", () => planDialog());
  document.querySelectorAll("[data-admin-plan]").forEach((button) => button.onclick = () => planDialog(state.admin.plans.find((plan) => plan.name === button.dataset.adminPlan)));
  document.querySelectorAll("[data-admin-user]").forEach((button) => button.onclick = () => userDialog(state.admin.users.find((user) => user.user_id === Number(button.dataset.adminUser))));
  document.querySelector("[data-admin-code-new]")?.addEventListener("click", () => codeDialog());
  document.querySelectorAll("[data-admin-code]").forEach((button) => button.onclick = () => codeDialog(state.admin.codes.find((code) => code.code === button.dataset.adminCode)));
  document.querySelector("[data-admin-search]")?.addEventListener("submit", (event) => { event.preventDefault(); loadAdmin(new FormData(event.currentTarget).get("q")); });
  document.querySelectorAll("[data-admin-tx]").forEach((button) => button.onclick = () => transactionDialog(state.admin.transactions.find((tx) => tx.id === Number(button.dataset.adminTx)), button.dataset.status));
  document.querySelectorAll("[data-admin-ticket]").forEach((button) => button.onclick = async () => { try { await api.adminUpdateSupport(button.dataset.adminTicket, button.dataset.status); await loadAdmin(); toast("Đã cập nhật hỗ trợ"); } catch (error) { toast(error.message, "error"); } });
  document.querySelectorAll("[data-admin-reply]").forEach((button) => button.onclick = () => replyDialog(state.admin.tickets.find((ticket) => ticket.id === Number(button.dataset.adminReply))));
  document.querySelector("[data-admin-settings]")?.addEventListener("submit", async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); const features = {}; ["tv","planToken","vipToken","freeCookie","giftcode","deposit","support"].forEach((key) => { features[key] = form.has(`feature_${key}`); }); try { await api.adminSaveSettings({maintenance: form.has("maintenance"), announcement: form.get("announcement"), features}); await loadAdmin(); toast("Đã lưu cấu hình hệ thống"); } catch (error) { toast(error.message, "error"); } });
  document.querySelectorAll("[data-admin-inventory]").forEach((button) => button.onclick = () => inventoryDialog(button.dataset.adminInventory));
  document.querySelectorAll("[data-admin-cleanup]").forEach((button) => button.onclick = async () => { if (!confirm("Chỉ xóa các mục đã dùng, tiếp tục?")) return; try { const result = await api.adminCleanupInventory(button.dataset.adminCleanup); await loadAdmin(); toast(`Đã dọn ${result.deleted} mục đã dùng`); } catch (error) { toast(error.message, "error"); } });
  document.querySelectorAll("[data-admin-order]").forEach((button) => button.onclick = () => orderDialog(state.admin.orders.find((order) => order.id === Number(button.dataset.adminOrder))));
}
