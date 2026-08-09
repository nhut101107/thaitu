import {api} from "./api.js";
import {state, subscribe, update} from "./state.js";
import {bottomNav, header, toast} from "./components.js";
import {accountView, addToCart, homeView, openCart, openOrder, openProduct, ordersView, storeView, toolsView} from "./views.js";

const tg = window.Telegram?.WebApp;
const app = document.querySelector("#app");
const loading = document.querySelector("#loading");
const content = document.querySelector("#content");
let searchTimer;

if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor("#f3f7f5");
  tg.setBackgroundColor("#f3f7f5");
}

function render() {
  if (!state.bootstrap) return;
  document.querySelector("#header").innerHTML = header(state.bootstrap.user);
  document.querySelector("#bottom-nav").innerHTML = bottomNav(state.route);
  content.innerHTML = ({home: homeView, store: storeView, orders: ordersView, tools: toolsView, account: accountView}[state.route] || homeView)();
  bindEvents();
}

subscribe(render);

async function loadProducts() {
  const result = await api.products({q: state.search, category: state.category, sort: state.sort});
  update({products: result.items, categories: result.categories});
}

async function loadOrders() {
  content.innerHTML = ordersView(true);
  const result = await api.orders();
  update({orders: result.items});
}

async function navigate(route) {
  update({route});
  window.scrollTo({top: 0, behavior: "smooth"});
  if (route === "orders") await loadOrders();
}

function sendBotAction(action) {
  if (!tg?.initData) return toast("Hãy mở Mini App bên trong Telegram", "error");
  tg.sendData(JSON.stringify({type: "open_bot_action", action}));
  tg.HapticFeedback?.impactOccurred("light");
  tg.close();
}

function bindEvents() {
  document.querySelectorAll("[data-route]").forEach((node) => node.onclick = () => navigate(node.dataset.route));
  document.querySelectorAll("[data-product]").forEach((node) => node.onclick = (event) => { if (!event.target.closest("[data-add]")) openProduct(node.dataset.product); });
  document.querySelectorAll("[data-add]").forEach((node) => node.onclick = (event) => { event.stopPropagation(); addToCart(node.dataset.add); });
  document.querySelectorAll("[data-order]").forEach((node) => node.onclick = () => openOrder(node.dataset.order));
  document.querySelectorAll("[data-bot-action]").forEach((node) => node.onclick = () => sendBotAction(node.dataset.botAction));
  document.querySelectorAll("[data-action='cart']").forEach((node) => node.onclick = openCart);
  document.querySelectorAll("[data-action='deposit']").forEach((node) => node.onclick = () => sendBotAction("deposit_main"));
  document.querySelectorAll("[data-action='support']").forEach((node) => node.onclick = () => sendBotAction("report_error"));
  document.querySelectorAll("[data-action='search']").forEach((node) => node.onclick = () => navigate("store").then(() => document.querySelector("#product-search")?.focus()));
  document.querySelectorAll("[data-action='reload-orders']").forEach((node) => node.onclick = loadOrders);
  document.querySelectorAll("[data-category]").forEach((node) => node.onclick = () => { state.category = node.dataset.category; loadProducts(); });
  const search = document.querySelector("#product-search");
  if (search) search.oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.search = search.value.trim(); loadProducts(); }, 300); };
  const clear = document.querySelector("[data-clear-search]"); if (clear) clear.onclick = () => { state.search = ""; loadProducts(); };
  const sort = document.querySelector("#sort-products"); if (sort) sort.onchange = () => { state.sort = sort.value; loadProducts(); };
}

async function boot() {
  const message = document.querySelector("#loading-message");
  const retry = document.querySelector("#retry");
  retry.classList.add("hidden");
  message.textContent = "Đang xác thực Telegram...";
  try {
    if (!tg?.initData) throw new Error("Vui lòng mở Mini App từ nút trong bot Telegram.");
    const [bootstrap, products, cart] = await Promise.all([api.bootstrap(), api.products(), api.cart()]);
    state.bootstrap = bootstrap;
    state.products = products.items;
    state.categories = products.categories;
    state.cart = cart;
    loading.classList.add("fade-out");
    setTimeout(() => { loading.classList.add("hidden"); app.classList.remove("hidden"); render(); }, 220);
  } catch (error) {
    message.textContent = error.message;
    retry.classList.remove("hidden");
    retry.onclick = boot;
  }
}

boot();
