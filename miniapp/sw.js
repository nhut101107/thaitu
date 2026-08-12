const CACHE_VERSION = "shop-mmo-static-v5";
const STATIC_ASSETS = [
  "/",
  "/manifest.json",
  "/assets/styles.css",
  "/assets/modern.css?v=1",
  "/assets/tools.css?v=4",
  "/assets/theme.css?v=2",
  "/assets/app.js?v=17",
  "/assets/api.js?v=17",
  "/assets/state.js",
  "/assets/i18n.js",
  "/assets/components.js?v=17",
  "/assets/views.js?v=17",
  "/assets/admin.js?v=17",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_VERSION).then((cache) => cache.addAll(STATIC_ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_VERSION).map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/") || url.pathname.startsWith("/uploads/")) return;
  event.respondWith(caches.match(event.request).then((cached) => {
    const network = fetch(event.request).then((response) => {
      if (response.ok) {
        const copy = response.clone();
        caches.open(CACHE_VERSION).then((cache) => cache.put(event.request, copy));
      }
      return response;
    });
    return cached || network;
  }).catch(() => caches.match(event.request)));
});
