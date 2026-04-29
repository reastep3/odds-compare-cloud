// Arbyx — service worker (deliberately minimal, v3+).
//
// Why no caching: the v1/v2 caching strategies caused stale-content bugs
// after deploys. For an app that primarily serves live data anyway, the
// browser's own HTTP cache + ETag handling is plenty. The SW exists only
// because PWA installability requires one.
//
// Side effects on install/activate:
//   • Wipes any existing arbyx-shell-* caches left by older SW versions.
//   • Calls skipWaiting + clients.claim so a new SW takes over immediately.
//   • The page also listens for controllerchange and reloads itself (see
//     app.js), so deploys are visible after a single refresh.

const CACHE_PREFIX = "arbyx-shell-";

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k.startsWith(CACHE_PREFIX)).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// Allow the page to ask the waiting SW to take over immediately.
self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

// Pass every request straight to the network. No interception, no cache.
self.addEventListener("fetch", () => {});
