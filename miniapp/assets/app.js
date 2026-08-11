import {api} from "./api.js?v=13";
import {state, subscribe, update} from "./state.js";
import {bottomNav, header, toast} from "./components.js?v=13";
import {accountView, addToCart, claimFreeCookie, homeView, openCart, openCheckin, openDeposit, openGiftcode, openHelp, openMissions, openNftoken, openNotifications, openOrder, openProduct, openReferral, openSupport, openTvLogin, ordersView, storeView, toolsView} from "./views.js?v=13";
import {adminView, bindAdminEvents, loadAdmin} from "./admin.js?v=13";

const tg = window.Telegram?.WebApp;
const app = document.querySelector("#app");
const loading = document.querySelector("#loading");
const content = document.querySelector("#content");
let searchTimer;
let deferredInstallPrompt;

window.addEventListener("beforeinstallprompt", (event) => { event.preventDefault(); deferredInstallPrompt = event; });

if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor("#070707");
  tg.setBackgroundColor("#070707");
}

function render() {
  if (!state.bootstrap) return;
  document.querySelector("#header").innerHTML = header(state.bootstrap.user);
  document.querySelector("#bottom-nav").innerHTML = bottomNav(state.route);
  const view = ({home: homeView, store: storeView, orders: ordersView, tools: toolsView, account: accountView, admin: adminView}[state.route] || homeView)();
  const copyright = typeof state.bootstrap.copyright === "string" ? state.bootstrap.copyright : (state.bootstrap.copyright?.text || "© mnhut - NFToken Pro");
  content.innerHTML = `${view}<footer class="site-copyright">${copyright}</footer>`;
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

async function loadTools() {
  const result = await api.toolsStatus();
  update({tools: result});
}

async function navigate(route) {
  update({route});
  window.scrollTo({top: 0, behavior: "smooth"});
  if (route === "orders") await loadOrders();
  if (route === "tools") await loadTools();
  if (route === "admin") await loadAdmin();
}

function bindEvents() {
  document.querySelectorAll("[data-route]").forEach((node) => node.onclick = () => navigate(node.dataset.route));
  document.querySelectorAll("[data-product]").forEach((node) => node.onclick = (event) => { if (!event.target.closest("[data-add]")) openProduct(node.dataset.product); });
  document.querySelectorAll("[data-add]").forEach((node) => node.onclick = (event) => { event.stopPropagation(); addToCart(node.dataset.add); });
  document.querySelectorAll("[data-order]").forEach((node) => node.onclick = () => openOrder(node.dataset.order));
  const toolActions = {tv: openTvLogin, "plan-token": () => openNftoken("plan"), "vip-token": () => openNftoken("vip"), "free-cookie": claimFreeCookie, checkin: openCheckin, missions: openMissions, referral: openReferral, giftcode: openGiftcode, deposit: openDeposit, support: openSupport, help: openHelp};
  document.querySelectorAll("[data-tool]").forEach((node) => node.onclick = () => toolActions[node.dataset.tool]?.());
  document.querySelectorAll("[data-action='cart']").forEach((node) => node.onclick = openCart);
  document.querySelectorAll("[data-action='deposit']").forEach((node) => node.onclick = openDeposit);
  document.querySelectorAll("[data-action='support']").forEach((node) => node.onclick = openSupport);
  document.querySelectorAll("[data-action='notifications']").forEach((node) => node.onclick = openNotifications);
  document.querySelectorAll("[data-action='install']").forEach((node) => node.onclick = async () => { if (!deferredInstallPrompt) return toast("Trình duyệt chưa hỗ trợ cài PWA"); deferredInstallPrompt.prompt(); await deferredInstallPrompt.userChoice; deferredInstallPrompt = null; });
  document.querySelector("[data-language]")?.addEventListener("change", async (event) => { try { await api.setLanguage(event.target.value); state.bootstrap.user.language = event.target.value; render(); } catch (error) { toast(error.message, "error"); } });
  document.querySelectorAll("[data-action='search']").forEach((node) => node.onclick = () => navigate("store").then(() => document.querySelector("#product-search")?.focus()));
  document.querySelectorAll("[data-action='reload-orders']").forEach((node) => node.onclick = loadOrders);
  document.querySelectorAll("[data-category]").forEach((node) => node.onclick = () => { state.category = node.dataset.category; loadProducts(); });
  const search = document.querySelector("#product-search");
  if (search) search.oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.search = search.value.trim(); loadProducts(); }, 300); };
  const clear = document.querySelector("[data-clear-search]"); if (clear) clear.onclick = () => { state.search = ""; loadProducts(); };
  const sort = document.querySelector("#sort-products"); if (sort) sort.onchange = () => { state.sort = sort.value; loadProducts(); };
  bindAdminEvents();
}

async function boot() {
  const message = document.querySelector("#loading-message");
  const retry = document.querySelector("#retry");
  retry.classList.add("hidden");
  message.textContent = "Đang xác thực Telegram...";
  try {
    if (!tg?.initData) throw new Error("Vui lòng mở Mini App từ nút trong bot Telegram.");
    const [bootstrap, products, cart, notifications] = await Promise.all([api.bootstrap(), api.products(), api.cart(), api.notifications()]);
    state.bootstrap = bootstrap;
    state.products = products.items;
    state.categories = products.categories;
    state.cart = cart;
    state.notificationUnread = notifications.unread || 0;
    loading.classList.add("fade-out");
    setTimeout(() => { loading.classList.add("hidden"); app.classList.remove("hidden"); render(); }, 220);
  } catch (error) {
    message.textContent = error.message;
    retry.classList.remove("hidden");
    retry.onclick = boot;
  }
}

boot();

setInterval(async () => { if (!state.bootstrap) return; try { const result = await api.notifications(); update({notificationUnread: result.unread || 0}); } catch {} }, 60000);

if ("serviceWorker" in navigator && !window.Telegram?.WebApp) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}
