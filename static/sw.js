// Arbyx — service worker.
//
// Strategy:
//   • Network-first for the app shell (HTML / JS / CSS) so deploys take
//     effect on the very next page load. Cache is kept as offline fallback.
//   • Cache-first for icons / manifest (these rarely change).
//   • Bypass entirely for /api/* — odds + backtest results must always
//     hit the live backend.
//
// IMPORTANT: bump CACHE name on every release that changes static assets.
// On activate we delete every cache that doesn't match the current name,
// which is what evicts stale shells.

const CACHE = "arbyx-shell-v2";
const SHELL = [
  "/",
  "/static/style.css",
  "/static/app.js",
  "/static/icon.svg",
  "/manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // API: never cache, always live.
  if (url.pathname.startsWith("/api/")) return;

  // App shell (HTML / CSS / JS): network-first, fall back to cache offline.
  const isShell =
    url.pathname === "/" ||
    url.pathname.endsWith(".html") ||
    url.pathname.endsWith(".js")   ||
    url.pathname.endsWith(".css");

  if (isShell) {
    event.respondWith(
      fetch(req).then((res) => {
        if (res && res.status === 200 && res.type === "basic") {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(req, clone));
        }
        return res;
      }).catch(() => caches.match(req))
    );
    return;
  }

  // Everything else (icons, manifest): cache-first.
  event.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res && res.status === 200 && res.type === "basic") {
        const clone = res.clone();
        caches.open(CACHE).then((c) => c.put(req, clone));
      }
      return res;
    }))
  );
});
