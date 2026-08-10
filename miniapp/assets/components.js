const money = new Intl.NumberFormat("vi-VN");
export const formatMoney = (value) => `${money.format(Number(value || 0))} ₫`;
export const escapeHtml = (value = "") => String(value).replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));

export function icon(name) {
  const paths = {
    home: '<path d="M3 11.5 12 4l9 7.5V21h-6v-6H9v6H3z"/>',
    store: '<path d="M5 8h14l-1 13H6L5 8Zm3 0a4 4 0 0 1 8 0"/>',
    orders: '<path d="M5 3h14v18H5zM8 8h8M8 12h8M8 16h5"/>',
    tools: '<path d="M4 5h6v6H4zM14 5h6v6h-6zM4 15h6v6H4zM14 15h6v6h-6z"/>',
    account: '<circle cx="12" cy="8" r="4"/><path d="M4 22a8 8 0 0 1 16 0"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m16.5 16.5 4 4"/>',
    cart: '<path d="M3 4h2l2 11h10l3-8H6M9 20h.01M17 20h.01"/>',
    wallet: '<rect x="3" y="6" width="18" height="14" rx="3"/><path d="M16 11h5v5h-5zM7 6V4h10v2"/>',
    shield: '<path d="M12 2 20 5v6c0 5-3.4 9-8 11-4.6-2-8-6-8-11V5z"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    arrow: '<path d="m9 18 6-6-6-6"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V4H4v12h4"/>',
  };
  return `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.arrow}</svg>`;
}

export function header(user) {
  const initials = escapeHtml((user.firstName || "N")[0].toUpperCase());
  const avatar = user.photoUrl ? `<img src="${escapeHtml(user.photoUrl)}" alt="">` : initials;
  return `<div class="topbar"><div class="logo"><span class="brand-mark small">N</span><div><b>NFToken</b><small>PREMIUM STORE</small></div></div><div class="header-actions"><button data-action="search" aria-label="Tìm kiếm">${icon("search")}</button><button class="avatar" data-route="account">${avatar}</button></div></div>`;
}

export function bottomNav(route) {
  const items = [["home","home","Trang chủ"],["store","store","Cửa hàng"],["orders","orders","Đơn hàng"],["tools","tools","Tiện ích"],["account","account","Tài khoản"]];
  return items.map(([key, glyph, label]) => `<button data-route="${key}" class="${route === key ? "active" : ""}">${icon(glyph)}<span>${label}</span></button>`).join("");
}

export function productCard(item) {
  const totalBenefits = (item.nftokenCredits || 0) + (item.credits || 0);
  const image = item.imageUrl ? `<img src="${escapeHtml(item.imageUrl)}" alt="" loading="lazy">` : `<div class="product-placeholder"><span>N</span><small>${totalBenefits} lượt</small></div>`;
  return `<article class="product-card" data-product="${item.id}"><div class="product-image">${image}${item.featured ? '<em>Nổi bật</em>' : ''}</div><h3>${escapeHtml(item.name)}</h3><p>${icon("shield")} ${item.available ? "Đang bán" : "Tạm hết"} · ${item.warrantyDays ? `${item.warrantyDays} ngày BH` : "Giao tự động"}</p><footer><strong>${formatMoney(item.price)}</strong><button data-add="${item.id}" aria-label="Thêm vào giỏ">${icon("plus")}</button></footer></article>`;
}

export function emptyState(title, text, action = "") {
  return `<div class="empty-state"><span class="brand-mark">N</span><h3>${escapeHtml(title)}</h3><p>${escapeHtml(text)}</p>${action}</div>`;
}

export function skeleton(count = 4) {
  return `<div class="product-grid">${Array.from({length: count}, () => '<div class="skeleton product-skeleton"><i></i><b></b><span></span></div>').join("")}</div>`;
}

export function toast(message, kind = "success") {
  const root = document.querySelector("#toast-root");
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  root.append(node);
  setTimeout(() => node.remove(), 2800);
}

export function modal(content, options = {}) {
  const root = document.querySelector("#modal-root");
  root.innerHTML = `<div class="modal-backdrop" data-close-modal><section class="modal-sheet" role="dialog" aria-modal="true"><button class="modal-close" data-close-modal>×</button>${content}</section></div>`;
  window.Telegram?.WebApp?.BackButton?.show();
  const close = () => {
    root.innerHTML = "";
    window.Telegram?.WebApp?.BackButton?.hide();
  };
  root.querySelectorAll("[data-close-modal]").forEach((el) => el.addEventListener("click", (event) => { if (event.target === el) close(); }));
  window.Telegram?.WebApp?.BackButton?.onClick(close);
  options.onOpen?.(root, close);
  return close;
}
