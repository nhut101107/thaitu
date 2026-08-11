const tg = window.Telegram?.WebApp;

export class ApiError extends Error {
  constructor(message, status, payload = {}) {
    super(message);
    this.status = status;
    this.payload = payload;
    this.reasonCode = payload.reason_code || "unknown_error";
  }
}

async function request(path, options = {}) {
  const isForm = options.body instanceof FormData;
  const timeoutMs = options.timeoutMs || (path === "/api/tools/nftoken" ? 60000 : 30000);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  let payload;
  try {
    response = await fetch(path, {
      ...options,
      signal: options.signal || controller.signal,
      headers: {
        ...(isForm ? {} : {"Content-Type": "application/json"}),
        "X-Telegram-Init-Data": tg?.initData || "",
        ...(options.headers || {}),
      },
    });
  } catch (_error) {
    clearTimeout(timeout);
    throw new ApiError("Không thể kết nối máy chủ", 0, {reason_code: "network_error"});
  }
  payload = await response.json().catch(() => ({}));
  clearTimeout(timeout);
  if (!response.ok || !payload.ok) {
    throw new ApiError(payload.error || "Không thể kết nối máy chủ", response.status, payload);
  }
  return payload;
}

export const api = {
  request,
  bootstrap: () => request("/api/bootstrap"),
  products: (params = {}) => request(`/api/products?${new URLSearchParams(params)}`),
  product: (id) => request(`/api/products/${id}`),
  cart: () => request("/api/cart"),
  setCart: (id, quantity) => request(`/api/cart/${id}`, {method: "PUT", body: JSON.stringify({quantity})}),
  checkout: (idempotencyKey, promoCode = "") => request("/api/checkout", {method: "POST", body: JSON.stringify({idempotencyKey, promoCode})}),
  orders: () => request("/api/orders"),
  order: (id) => request(`/api/orders/${id}`),
  transactions: () => request("/api/transactions"),
  toolsStatus: () => request("/api/tools/status"),
  freeCookie: () => request("/api/tools/free-cookie", {method: "POST", body: "{}"}),
  referral: () => request("/api/referral"),
  checkin: () => request("/api/checkin", {method: "POST", body: "{}"}),
  checkinHistory: () => request("/api/checkin/history"),
  nftoken: (mode, quantity = 1) => request("/api/tools/nftoken", {method: "POST", body: JSON.stringify({mode, quantity})}),
  tvLogin: (code) => request("/api/tools/tv-login", {method: "POST", body: JSON.stringify({code})}),
  giftcode: (code) => request("/api/giftcode", {method: "POST", body: JSON.stringify({code})}),
  deposit: (amount) => request("/api/deposits", {method: "POST", body: JSON.stringify({amount})}),
  submitDeposit: (id) => request(`/api/deposits/${id}/submit`, {method: "POST", body: "{}"}),
  support: (message) => request("/api/support", {method: "POST", body: JSON.stringify({message})}),
  referralLeaderboard: (period = "WEEK", limit = 10) => request(`/api/referral/leaderboard?period=${period}&limit=${limit}`),
  flashSales: () => request("/api/flash-sales"),
  missions: () => request("/api/missions"),
  claimMission: (id) => request(`/api/missions/${id}/claim`, {method: "POST", body: "{}"}),
  notifications: () => request("/api/notifications"),
  markNotificationsRead: (ids = [], all = false) => request("/api/notifications/read", {method: "POST", body: JSON.stringify({ids, all})}),
  getLanguage: () => request("/api/preferences/language"),
  setLanguage: (language) => request("/api/preferences/language", {method: "PUT", body: JSON.stringify({language})}),
  adminDashboard: (q = "") => request(`/api/admin/dashboard?${new URLSearchParams({q})}`),
  adminCreateProduct: (value) => request("/api/admin/products", {method: "POST", body: JSON.stringify(value)}),
  adminUpdateProduct: (id, value) => request(`/api/admin/products/${id}`, {method: "PUT", body: JSON.stringify(value)}),
  adminDeleteProduct: (id) => request(`/api/admin/products/${id}`, {method: "DELETE", body: "{}"}),
  adminProviders: () => request("/api/admin/providers"),
  adminCreateProvider: (value) => request("/api/admin/providers", {method: "POST", body: JSON.stringify(value)}),
  adminUpdateProvider: (id, value) => request(`/api/admin/providers/${id}`, {method: "PUT", body: JSON.stringify(value)}),
  adminTestProvider: (id) => request(`/api/admin/providers/${id}/test`, {method: "POST", body: "{}"}),
  adminSyncProvider: (id) => request(`/api/admin/providers/${id}/sync`, {method: "POST", body: "{}"}),
  adminRanks: () => request("/api/admin/ranks"),
  adminUpdateRank: (rank, value) => request(`/api/admin/ranks/${encodeURIComponent(rank)}`, {method: "PUT", body: JSON.stringify(value)}),
  adminCopyright: () => request("/api/admin/copyright"),
  adminUpdateCopyright: (value) => request("/api/admin/copyright", {method: "PUT", body: JSON.stringify(value)}),
  adminUploadBrand: (file) => { const body = new FormData(); body.append("file", file, file.name); return request("/api/admin/brand", {method: "POST", body}); },
  adminDeleteBrand: () => request("/api/admin/brand", {method: "DELETE", body: "{}"}),
  adminSavePlan: (name, value) => request(`/api/admin/plans/${encodeURIComponent(name)}`, {method: "PUT", body: JSON.stringify(value)}),
  adminUpdateUser: (id, value) => request(`/api/admin/users/${id}`, {method: "PUT", body: JSON.stringify(value)}),
  adminUpdateTransaction: (id, status, note = "") => request(`/api/admin/transactions/${id}`, {method: "PUT", body: JSON.stringify({status, note})}),
  adminSaveCode: (code, value) => request(`/api/admin/codes/${encodeURIComponent(code)}`, {method: "PUT", body: JSON.stringify(value)}),
  adminUpdateSupport: (id, status) => request(`/api/admin/support/${id}`, {method: "PUT", body: JSON.stringify({status})}),
  adminReplySupport: (id, message) => request(`/api/admin/support/${id}/reply`, {method: "POST", body: JSON.stringify({message})}),
  adminSaveSettings: (value) => request("/api/admin/settings", {method: "PUT", body: JSON.stringify(value)}),
  adminAddInventory: (kind, data) => request(`/api/admin/inventory/${kind}`, {method: "POST", body: JSON.stringify({data})}),
  adminUploadInventory: (kind, files) => { const body = new FormData(); const list = Array.isArray(files) ? files : [files]; list.forEach((file) => body.append("files", file, file.webkitRelativePath || file.name)); return request(`/api/admin/inventory/${kind}/upload`, {method: "POST", body}); },
  adminCleanupInventory: (kind) => request(`/api/admin/inventory/${kind}/cleanup`, {method: "POST", body: "{}"}),
  adminUpdateOrder: (id, value) => request(`/api/admin/orders/${id}`, {method: "PUT", body: JSON.stringify(value)}),
  adminFlashSales: () => request("/api/admin/flash-sales"),
  adminCreateFlashSale: (value) => request("/api/admin/flash-sales", {method: "POST", body: JSON.stringify(value)}),
  adminUpdateFlashSale: (id, value) => request(`/api/admin/flash-sales/${id}`, {method: "PUT", body: JSON.stringify(value)}),
  adminReferralLeaderboards: () => request("/api/admin/referral-leaderboards"),
  adminUpdateReferralLeaderboard: (period, value) => request(`/api/admin/referral-leaderboards/${period}`, {method: "PUT", body: JSON.stringify(value)}),
  adminReports: (params = {}) => request(`/api/admin/reports?${new URLSearchParams(params)}`),
  adminReportsCsv: async () => { const response = await fetch("/api/admin/reports?format=csv", {headers: {"X-Telegram-Init-Data": tg?.initData || ""}}); if (!response.ok) throw new ApiError("Không thể tải báo cáo", response.status); return response.blob(); },
  adminMissions: () => request("/api/admin/missions"),
  adminCreateMission: (value) => request("/api/admin/missions", {method: "POST", body: JSON.stringify(value)}),
  adminUpdateMission: (id, value) => request(`/api/admin/missions/${id}`, {method: "PUT", body: JSON.stringify(value)}),
  adminCreateNotification: (value) => request("/api/admin/notifications", {method: "POST", body: JSON.stringify(value)}),
};
