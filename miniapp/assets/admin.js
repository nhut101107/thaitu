import {api} from "./api.js";
import {state, update} from "./state.js";
import {emptyState, escapeHtml, formatMoney, modal, skeleton, toast} from "./components.js";

export async function loadAdmin(query = "") {
  try {
    const admin = await api.adminDashboard(query);
    update({admin});
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

    <details class="admin-section" open><summary><span><i>🛍</i><b>Sản phẩm cửa hàng</b><small>${data.products.length} sản phẩm</small></span><em>⌄</em></summary><div class="admin-section-body"><button class="button admin-add" data-admin-product-new>＋ Thêm sản phẩm</button>${adminRows(data.products, (item) => `<article class="admin-row"><div><b>${escapeHtml(item.name)}</b><small>${formatMoney(item.price)} · ⚡ ${item.nftokenCredits || 0} lượt NFToken · 🍪 ${item.credits} lượt Cookie VIP · ${item.available ? "Đang bán" : "Đã ẩn"}</small></div><button data-admin-product="${item.id}">Sửa</button></article>`, "Hãy tạo sản phẩm đầu tiên.")}</div></details>

    <details class="admin-section"><summary><span><i>⚡</i><b>Gói & hạn mức chức năng</b><small>NFToken và Cookie miễn phí mỗi ngày</small></span><em>⌄</em></summary><div class="admin-section-body"><button class="button admin-add" data-admin-plan-new>＋ Tạo gói hạn mức</button>${adminRows(data.plans, (plan) => `<article class="admin-row"><div><b>${escapeHtml(plan.name)}</b><small>${plan.tokens_max} NFToken/ngày · ${plan.cookies_max} Cookie/ngày</small></div><button data-admin-plan="${escapeHtml(plan.name)}">Sửa</button></article>`, "Chưa có gói hạn mức.")}</div></details>

    <details class="admin-section" open><summary><span><i>💸</i><b>Duyệt nạp tiền</b><small>${s.pendingDeposits} khách đã báo chuyển khoản</small></span><em>⌄</em></summary><div class="admin-section-body">${adminRows(data.transactions, (tx) => `<article class="admin-row transaction ${tx.status.toLowerCase()}"><div><b>#${tx.id} · ${formatMoney(tx.amount)}</b><small>ID ${tx.user_id}${tx.username ? ` · @${escapeHtml(tx.username)}` : ""}<br>${tx.transfer_note ? `Nội dung: ${escapeHtml(tx.transfer_note)} · ` : ""}${escapeHtml(tx.submitted_at || tx.created_at || "")}</small>${tx.review_note ? `<small class="review-note">${escapeHtml(tx.review_note)}</small>` : ""}</div>${tx.status === "PENDING" ? `<span><button class="approve" data-admin-tx="${tx.id}" data-status="APPROVED">Duyệt</button><button class="reject" data-admin-tx="${tx.id}" data-status="REJECTED">Từ chối</button></span>` : `<em class="tx-state">${tx.status === "AWAITING_PAYMENT" ? "Chưa báo chuyển" : escapeHtml(tx.status)}</em>`}</article>`, "Chưa có giao dịch nạp.")}</div></details>

    <details class="admin-section"><summary><span><i>🧾</i><b>Quản lý đơn hàng</b><small>${data.orders.length} đơn gần nhất</small></span><em>⌄</em></summary><div class="admin-section-body">${adminRows(data.orders, (order) => `<article class="admin-row"><div><b>#${order.id} · ${escapeHtml(order.plan_name)}</b><small>ID ${order.user_id} · ${formatMoney(order.price)} · ${escapeHtml(order.status || "COMPLETED")} · ${escapeHtml(order.date)}</small></div><button data-admin-order="${order.id}">Sửa</button></article>`, "Chưa có đơn hàng.")}</div></details>

    <details class="admin-section"><summary><span><i>👤</i><b>Quản lý người dùng</b><small>Số dư, lượt, gói và khóa tài khoản</small></span><em>⌄</em></summary><div class="admin-section-body"><form class="admin-search" data-admin-search><input name="q" placeholder="Nhập Telegram ID hoặc username"><button>Tìm</button></form>${adminRows(data.users, (user) => `<article class="admin-row"><div><b>${user.username ? `@${escapeHtml(user.username)}` : "Không username"}</b><small>ID ${user.user_id} · ${formatMoney(user.balance)} · ⚡ ${user.nftoken_credits || 0} NFToken · 🍪 ${user.credits} VIP · ${escapeHtml(user.plan_name)}${user.is_banned ? " · ĐÃ KHÓA" : ""}</small></div><button data-admin-user="${user.user_id}">Sửa</button></article>`, "Không tìm thấy người dùng.")}</div></details>

    <details class="admin-section"><summary><span><i>🎟</i><b>Mã quà tặng</b><small>Tạo và cập nhật giftcode</small></span><em>⌄</em></summary><div class="admin-section-body"><button class="button admin-add" data-admin-code-new>＋ Tạo giftcode</button>${adminRows(data.codes, (code) => `<article class="admin-row"><div><b>${escapeHtml(code.code)}</b><small>${formatMoney(code.amount)} · còn ${code.uses} lượt</small></div><button data-admin-code="${escapeHtml(code.code)}">Sửa</button></article>`, "Chưa có giftcode.")}</div></details>

    <details class="admin-section"><summary><span><i>🛟</i><b>Yêu cầu hỗ trợ</b><small>${s.openTickets} yêu cầu chưa đóng</small></span><em>⌄</em></summary><div class="admin-section-body">${adminRows(data.tickets, (ticket) => `<article class="admin-ticket"><header><b>#${ticket.id} · ID ${ticket.user_id}</b><em>${escapeHtml(ticket.status)}</em></header><p>${escapeHtml(ticket.message)}</p><small>${escapeHtml(ticket.created_at)}</small><div><button data-admin-reply="${ticket.id}">Trả lời</button><button data-admin-ticket="${ticket.id}" data-status="${ticket.status === "OPEN" ? "CLOSED" : "OPEN"}">${ticket.status === "OPEN" ? "Đã xử lý" : "Mở lại"}</button></div></article>`, "Chưa có yêu cầu hỗ trợ.")}</div></details>

    <details class="admin-section"><summary><span><i>🧭</i><b>Nhật ký quản trị</b><small>100 thao tác gần nhất</small></span><em>⌄</em></summary><div class="admin-section-body">${adminRows(data.audit, (entry) => `<article class="audit-row"><b>${escapeHtml(entry.action)}</b><span>${escapeHtml(entry.target)}</span><p>${escapeHtml(entry.details || "Không có chi tiết")}</p><small>${escapeHtml(entry.created_at)} · Admin ${entry.admin_id}</small></article>`, "Chưa có thao tác quản trị.")}</div></details>
  </div>`;
}

function productDialog(item = null) {
  modal(`<div class="eyebrow">QUẢN LÝ CỬA HÀNG</div><h2>${item ? "Sửa sản phẩm" : "Thêm sản phẩm"}</h2><form data-product-form>
    <label class="field">Tên sản phẩm<input name="name" maxlength="80" value="${escapeHtml(item?.name || "")}" required></label>
    <label class="field">Giá bán<input name="price" type="number" min="0" value="${item?.price || 0}" required></label>
    <div class="field-pair"><label class="field">⚡ Lượt tạo link NFToken<input name="nftokenCredits" type="number" min="0" value="${item?.nftokenCredits || 0}" required></label><label class="field">🍪 Lượt lấy Cookie VIP<input name="credits" type="number" min="0" value="${item?.credits || 0}" required></label></div>
    <label class="field">Danh mục<input name="category" maxlength="80" value="${escapeHtml(item?.category || "Gói Cookie VIP")}" required></label>
    <label class="field">Mô tả<textarea name="description" maxlength="1000">${escapeHtml(item?.description || "")}</textarea></label>
    <label class="field">Link ảnh HTTPS<input name="imageUrl" type="url" value="${escapeHtml(item?.imageUrl || "")}" placeholder="https://..."></label>
    <label class="field">Số ngày bảo hành<input name="warrantyDays" type="number" min="0" max="3650" value="${item?.warrantyDays || 0}"></label>
    <div class="check-row"><label><input name="featured" type="checkbox" ${item?.featured ? "checked" : ""}> Sản phẩm nổi bật</label><label><input name="active" type="checkbox" ${item?.available !== false ? "checked" : ""}> Đang bán</label></div>
    <button class="button wide">${item ? "Lưu thay đổi" : "Tạo sản phẩm"}</button></form>`, {onOpen(root, close) {
      root.querySelector("[data-product-form]").onsubmit = async (event) => {
        event.preventDefault(); const form = new FormData(event.currentTarget);
        const value = {name: form.get("name"), price: Number(form.get("price")), nftokenCredits: Number(form.get("nftokenCredits")), credits: Number(form.get("credits")), category: form.get("category"), description: form.get("description"), imageUrl: form.get("imageUrl"), warrantyDays: Number(form.get("warrantyDays")), featured: form.has("featured"), active: form.has("active")};
        try { item ? await api.adminUpdateProduct(item.id, value) : await api.adminCreateProduct(value); close(); await loadAdmin(); toast("Đã lưu sản phẩm"); } catch (error) { toast(error.message, "error"); }
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
  modal(`<div class="eyebrow">KHO ${label.toUpperCase()}</div><h2>Tải và lọc Cookie live</h2><div class="upload-zone"><span>📁</span><b>Chọn file .txt, .zip hoặc .rar</b><small>ZIP/RAR tối đa 10MB · không giới hạn · chỉ đọc các file TXT</small><input type="file" name="file" accept=".txt,.zip,.rar,text/plain,application/zip,application/x-rar-compressed" data-cookie-file></div><button class="button wide" data-upload-cookies>Kiểm tra live & lưu vào kho</button><div class="upload-progress hidden" data-upload-progress><i></i><b>Đang kiểm tra Cookie...</b><small>Giữ Mini App mở, quá trình có thể mất vài phút.</small></div><details class="manual-cookie"><summary>Hoặc nhập thủ công</summary><form data-inventory-form><label class="field">Dữ liệu<textarea name="data" maxlength="60000" required placeholder="NetflixId=...\n---\nNetflixId=..."></textarea></label><button class="button wide">Thêm không kiểm tra live</button></form></details>`, {onOpen(root, close) {
    const input = root.querySelector("[data-cookie-file]");
    input.onchange = () => { if (input.files[0]) root.querySelector(".upload-zone b").textContent = input.files[0].name; };
    root.querySelector("[data-upload-cookies]").onclick = async (event) => { const file = input.files[0]; if (!file) return toast("Hãy chọn file TXT, ZIP hoặc RAR", "error"); const done = busyButton(event.currentTarget, "Đang lọc Cookie live..."); root.querySelector("[data-upload-progress]").classList.remove("hidden"); try { const result = await api.adminUploadInventory(kind, file); close(); await loadAdmin(); toast(`Đã kiểm tra ${result.checked}: thêm ${result.added} live, ${result.dead} lỗi, ${result.duplicates} trùng`); } catch (error) { toast(error.message, "error"); root.querySelector("[data-upload-progress]").classList.add("hidden"); done(); } };
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
  document.querySelector("[data-admin-product-new]")?.addEventListener("click", () => productDialog());
  document.querySelectorAll("[data-admin-product]").forEach((button) => button.onclick = () => productDialog(state.admin.products.find((item) => item.id === Number(button.dataset.adminProduct))));
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
