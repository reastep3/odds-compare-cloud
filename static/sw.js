// Arbyx — minimal service worker.
// v1: NO caching of API responses (always fresh odds). Just a network-first
// shell so "Add to Home Screen" works as a real installable PWA.
// Static assets get cached so the app shell loads instantly on revisit.

const CACHE = "arbyx-shell-v1";
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
  const url = new URL(event.request.url);

  // Never cache API. Always go to network.
  if (url.pathname.startsWith("/api/")) return;

  // Static shell: cache-first with network fallback (and update cache).
  event.respondWith(
    caches.match(event.request).then((hit) => {
      const fetchPromise = fetch(event.request).then((res) => {
        if (res && res.status === 200 && res.type === "basic") {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(event.request, clone));
        }
        return res;
      }).catch(() => hit);
      return hit || fetchPromise;
    })
  );
});
