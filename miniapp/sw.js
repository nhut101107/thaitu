const CACHE_VERSION = "shop-mmo-static-v9";
const STATIC_ASSETS = [
  "/",
  "/manifest.json",
  "/assets/styles.css",
  "/assets/modern.css?v=2",
  "/assets/tools.css?v=4",
  "/assets/theme.css?v=3",
  "/assets/app.js?v=20",
  "/assets/api.js?v=19",
  "/assets/state.js",
  "/assets/i18n.js?v=3",
  "/assets/components.js?v=19",
  "/assets/views.js?v=20",
  "/assets/admin.js?v=19",
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
