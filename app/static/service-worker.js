const CACHE_NAME = "bossfind-radar-v20260915-layout";
const CORE_ASSETS = [
  "/",
  "/static/styles.css?v=20260915-radar-layout",
  "/static/mobile-fixes.css?v=20260915-radar-layout",
  "/static/app.js?v=20260914-live-choice",
  "/static/manifest.webmanifest",
  "/static/icons/icon-192.svg",
  "/static/icons/icon-512.svg"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(CORE_ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(fetch(request).catch(() => new Response("[]", { headers: { "Content-Type": "application/json" } })));
    return;
  }
  event.respondWith(caches.match(request).then((cached) => cached || fetch(request)));
});
