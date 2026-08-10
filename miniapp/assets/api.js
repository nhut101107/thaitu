const tg = window.Telegram?.WebApp;

export class ApiError extends Error {
  constructor(message, status, payload = {}) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

async function request(path, options = {}) {
  const isForm = options.body instanceof FormData;
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(isForm ? {} : {"Content-Type": "application/json"}),
      "X-Telegram-Init-Data": tg?.initData || "",
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || !payload.ok) {
    throw new ApiError(payload.error || "Không thể kết nối máy chủ", response.status, payload);
  }
  return payload;
}

export const api = {
  bootstrap: () => request("/api/bootstrap"),
  products: (params = {}) => request(`/api/products?${new URLSearchParams(params)}`),
  product: (id) => request(`/api/products/${id}`),
  cart: () => request("/api/cart"),
  setCart: (id, quantity) => request(`/api/cart/${id}`, {method: "PUT", body: JSON.stringify({quantity})}),
  checkout: (idempotencyKey) => request("/api/checkout", {method: "POST", body: JSON.stringify({idempotencyKey})}),
  orders: () => request("/api/orders"),
  order: (id) => request(`/api/orders/${id}`),
  transactions: () => request("/api/transactions"),
  toolsStatus: () => request("/api/tools/status"),
  freeCookie: () => request("/api/tools/free-cookie", {method: "POST", body: "{}"}),
  nftoken: (mode, quantity = 1) => request("/api/tools/nftoken", {method: "POST", body: JSON.stringify({mode, quantity})}),
  tvLogin: (code) => request("/api/tools/tv-login", {method: "POST", body: JSON.stringify({code})}),
  giftcode: (code) => request("/api/giftcode", {method: "POST", body: JSON.stringify({code})}),
  deposit: (amount) => request("/api/deposits", {method: "POST", body: JSON.stringify({amount})}),
  submitDeposit: (id) => request(`/api/deposits/${id}/submit`, {method: "POST", body: "{}"}),
  support: (message) => request("/api/support", {method: "POST", body: JSON.stringify({message})}),
  adminDashboard: (q = "") => request(`/api/admin/dashboard?${new URLSearchParams({q})}`),
  adminCreateProduct: (value) => request("/api/admin/products", {method: "POST", body: JSON.stringify(value)}),
  adminUpdateProduct: (id, value) => request(`/api/admin/products/${id}`, {method: "PUT", body: JSON.stringify(value)}),
  adminSavePlan: (name, value) => request(`/api/admin/plans/${encodeURIComponent(name)}`, {method: "PUT", body: JSON.stringify(value)}),
  adminUpdateUser: (id, value) => request(`/api/admin/users/${id}`, {method: "PUT", body: JSON.stringify(value)}),
  adminUpdateTransaction: (id, status, note = "") => request(`/api/admin/transactions/${id}`, {method: "PUT", body: JSON.stringify({status, note})}),
  adminSaveCode: (code, value) => request(`/api/admin/codes/${encodeURIComponent(code)}`, {method: "PUT", body: JSON.stringify(value)}),
  adminUpdateSupport: (id, status) => request(`/api/admin/support/${id}`, {method: "PUT", body: JSON.stringify({status})}),
  adminReplySupport: (id, message) => request(`/api/admin/support/${id}/reply`, {method: "POST", body: JSON.stringify({message})}),
  adminSaveSettings: (value) => request("/api/admin/settings", {method: "PUT", body: JSON.stringify(value)}),
  adminAddInventory: (kind, data) => request(`/api/admin/inventory/${kind}`, {method: "POST", body: JSON.stringify({data})}),
  adminUploadInventory: (kind, file) => { const body = new FormData(); body.append("file", file); return request(`/api/admin/inventory/${kind}/upload`, {method: "POST", body}); },
  adminCleanupInventory: (kind) => request(`/api/admin/inventory/${kind}/cleanup`, {method: "POST", body: "{}"}),
  adminUpdateOrder: (id, value) => request(`/api/admin/orders/${id}`, {method: "PUT", body: JSON.stringify(value)}),
};
