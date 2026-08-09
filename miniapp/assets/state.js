export const state = {
  bootstrap: null,
  products: [],
  categories: [],
  cart: {items: [], count: 0, total: 0},
  orders: [],
  route: "home",
  search: "",
  category: "",
  sort: "popular",
  busy: false,
};

const listeners = new Set();
export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
export function update(patch) {
  Object.assign(state, patch);
  listeners.forEach((listener) => listener(state));
}
