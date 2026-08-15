import {api} from "./api.js?v=19";
import {state, subscribe, update} from "./state.js";
import {bottomNav, header, toast} from "./components.js?v=19";
import {accountView, addToCart, claimFreeCookie, homeView, openCart, openCheckin, openDeliveries, openDeposit, openDevices, openGiftcode, openHelp, openMissions, openNftoken, openNotifications, openOrder, openProduct, openReferral, openSupport, openTvLogin, ordersView, storeView, toolsView} from "./views.js?v=20";
import {adminView, bindAdminEvents, loadAdmin} from "./admin.js?v=19";
import {translateDom, translateText} from "./i18n.js?v=3";

const tg = window.Telegram?.WebApp;
const app = document.querySelector("#app");
const loading = document.querySelector("#loading");
const content = document.querySelector("#content");
let searchTimer;
let deferredInstallPrompt;
let renderFrame = 0;
const PWA_SESSION_KEY = "shop_mmo_pwa_session";
const isTelegram = Boolean(tg?.initData);

function pwaSession() {
  try { return localStorage.getItem(PWA_SESSION_KEY) || ""; } catch { return ""; }
}

function consumePwaHash() {
  const match = new URLSearchParams(window.location.hash.replace(/^#/, "")).get("pwa_session");
  if (!match) return;
  try { localStorage.setItem(PWA_SESSION_KEY, match); } catch {}
  history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
}

window.addEventListener("beforeinstallprompt", (event) => { event.preventDefault(); deferredInstallPrompt = event; });
window.addEventListener("appinstalled", () => { deferredInstallPrompt = null; toast("Đã cài Shop MMO"); });

if (tg) {
  tg.ready();
  tg.expand();
  tg.setHeaderColor("#070707");
  tg.setBackgroundColor("#070707");
}

function render() {
  renderFrame = 0;
  if (!state.bootstrap) return;
  document.querySelector("#header").innerHTML = header(state.bootstrap.user);
  document.querySelector("#bottom-nav").innerHTML = bottomNav(state.route);
  const view = ({home: homeView, store: storeView, orders: ordersView, tools: toolsView, account: accountView, admin: adminView}[state.route] || homeView)();
  const copyright = typeof state.bootstrap.copyright === "string" ? state.bootstrap.copyright : (state.bootstrap.copyright?.text || "© mnhut - NFToken Pro");
  content.innerHTML = `${view}<footer class="site-copyright">${copyright}</footer>`;
  bindEvents();
  translateDom(document.querySelector("#app"));
}

function scheduleRender() {
  if (renderFrame) return;
  renderFrame = requestAnimationFrame(() => render());
}

subscribe(scheduleRender);

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
  window.scrollTo({top: 0, behavior: "auto"});
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
  document.querySelectorAll("[data-action='deliveries']").forEach((node) => node.onclick = openDeliveries);
  document.querySelectorAll("[data-action='devices']").forEach((node) => node.onclick = openDevices);
  document.querySelectorAll("[data-action='notifications']").forEach((node) => node.onclick = openNotifications);
  document.querySelectorAll("[data-action='install']").forEach((node) => node.onclick = installPwa);
  document.querySelector("[data-language]")?.addEventListener("change", async (event) => {
    const previous = state.bootstrap.user.language;
    const next = event.target.value;
    state.bootstrap.user.language = next;
    render();
    try { await api.setLanguage(next); }
    catch (error) { state.bootstrap.user.language = previous; render(); toast(error.message, "error"); }
  });
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
  consumePwaHash();
  message.textContent = "Đang xác thực Telegram...";
  try {
    if (!isTelegram && !pwaSession()) throw new Error("Hãy mở Shop MMO từ Telegram lần đầu để kích hoạt PWA.");
    const [bootstrap, products, cart, notifications] = await Promise.all([api.bootstrap(), api.products(), api.cart(), api.notifications()]);
    state.bootstrap = bootstrap;
    state.products = products.items;
    state.categories = products.categories;
    state.cart = cart;
    state.notificationUnread = notifications.unread || 0;
    loading.classList.add("fade-out");
    setTimeout(() => { loading.classList.add("hidden"); app.classList.remove("hidden"); render(); }, 220);
  } catch (error) {
    message.textContent = translateText(error.message);
    retry.classList.remove("hidden");
    retry.onclick = boot;
  }
}

boot();

setInterval(async () => { if (!state.bootstrap) return; try { const result = await api.notifications(); update({notificationUnread: result.unread || 0}); } catch {} }, 60000);

async function installPwa() {
  if (window.matchMedia("(display-mode: standalone)").matches) return toast("Shop MMO đã được cài trên thiết bị");
  if (deferredInstallPrompt) {
    deferredInstallPrompt.prompt();
    await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
    return;
  }
  if (isTelegram) {
    try {
      const result = await api.pwaSession();
      const target = new URL(window.location.href);
      target.hash = `pwa_session=${encodeURIComponent(result.token)}`;
      tg.openLink?.(target.toString());
      if (!tg.openLink) window.open(target.toString(), "_blank", "noopener");
    } catch (error) { toast(error.message, "error"); }
    return;
  }
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  toast(ios ? "Chọn Chia sẻ → Thêm vào Màn hình chính" : "Mở menu trình duyệt và chọn Cài đặt ứng dụng");
}

if ("serviceWorker" in navigator && !isTelegram) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}
