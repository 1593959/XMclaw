/*
 * XMclaw service worker — installs as a PWA so the app appears in
 * the user's app drawer / Home Screen and can be opened without
 * a browser tab. Sprint 1 Wave 4.
 *
 * Caching policy (intentionally minimal):
 *   * App shell (index.html, bootstrap.js, manifest, CSS, atoms) →
 *     stale-while-revalidate so first paint is offline-snappy.
 *   * /api/* and /agent/v2/* (WebSocket) → NEVER cached — they
 *     carry live state. Each navigation falls through to network.
 *   * On daemon-side update (new BOOT_VERSION cookie / query),
 *     bootstrap.js's cache-busting already keeps assets fresh; we
 *     just skip caching outright for any URL containing ``?v=`` or
 *     ``?bv=``.
 *
 * Push notifications stub — Sprint 1 Wave 4+ will use this to wake
 * the user when a long-running task completes or the proactive
 * agent fires a high-urgency proposal.
 */
const CACHE_VERSION = "xmclaw-v1";
const APP_SHELL_PATHS = [
  "/ui/",
  "/ui/chat",
  "/ui/bootstrap.js",
  "/ui/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) =>
      cache.addAll(APP_SHELL_PATHS).catch(() => {}),
    ),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)),
      ),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  // Don't intercept POSTs, WebSocket upgrades, API calls.
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.pathname.startsWith("/api/")
      || url.pathname.startsWith("/agent/v2/")
      || url.pathname.startsWith("/api/v2/media/")) {
    return;
  }
  // Cache-bust signal — let the network speak.
  if (url.search.includes("v=") || url.search.includes("bv=")) {
    return;
  }
  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req).then((res) => {
        if (res && res.ok) {
          const clone = res.clone();
          caches.open(CACHE_VERSION).then((cache) => cache.put(req, clone));
        }
        return res;
      }).catch(() => cached);  // offline → cached, if any
      return cached || network;
    }),
  );
});

// Push notifications — currently a placeholder; backend can publish
// proactive proposals via webPush once VAPID is configured.
self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : {};
  const title = data.title || "XMclaw";
  const options = {
    body: data.body || "",
    icon: "/ui/assets/xmclaw-icon-192.png",
    badge: "/ui/assets/xmclaw-icon-192.png",
    data: data.payload || {},
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window" }).then((clients) => {
      for (const c of clients) {
        if (c.url.includes("/ui/") && "focus" in c) return c.focus();
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow("/ui/chat");
      }
    }),
  );
});
