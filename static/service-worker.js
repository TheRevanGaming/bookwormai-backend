// Simple service worker to make Book Worm feel app-like and cache the shell.

const CACHE_NAME = "bookworm-cache-v1";

const URLS_TO_CACHE = [
  "/",
  "/static/index.html",
  "/static/manifest.json"
  // You can add CSS/JS files here if they are separate.
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(URLS_TO_CACHE).catch((err) => {
        console.warn("SW: cache addAll failed:", err);
      });
    })
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
        })
      )
    )
  );
});

// Network-first for API, cache-first for static shell
self.addEventListener("fetch", (event) => {
  const req = event.request;

  // Don’t try to cache /generate or /generate_image or /docs – those are API calls
  if (
    req.url.includes("/generate") ||
    req.url.includes("/generate_image") ||
    req.url.includes("/docs")
  ) {
    return;
  }

  // For everything else, try cache first, then network
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).catch(() => cached);
    })
  );
});
