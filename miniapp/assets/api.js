const tg = window.Telegram?.WebApp;

export class ApiError extends Error {
  constructor(message, status, payload = {}) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
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
};
