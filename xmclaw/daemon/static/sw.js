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
// 2026-05-31: bumped v1→v2 to PURGE the old cache. The previous
// policy cache-FIRST'd .js modules under a static version that never
// invalidated, so once a module was cached the SW served that stale
// (or partial/broken) copy FOREVER — even across daemon restarts —
// which blanked the whole app after any JS update. v2 is network-first
// for app code (see fetch handler) and the activate purge below wipes
// the poisoned v1 cache on first load of this file.
const CACHE_VERSION = "xmclaw-v2";
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

  // App CODE (HTML navigations + JS/CSS/MJS modules) → NETWORK-FIRST.
  // This is the fix for the blank-screen-after-update bug: a daemon
  // update must ALWAYS be picked up. We only fall back to cache when
  // the network genuinely fails (true offline). Caching code at all is
  // just a last-resort offline nicety — for a localhost daemon the
  // "network" is 127.0.0.1, so freshness >> the marginal cache speed-up.
  const isNavigation = req.mode === "navigate";
  const p = url.pathname;
  const isCode =
    isNavigation
    || p.endsWith(".js")
    || p.endsWith(".mjs")
    || p.endsWith(".css");
  if (isCode) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          // Only cache real, complete responses. Never cache an HTML
          // SPA-fallback that came back for a missing .js (wrong MIME
          // → import() would choke on the cached copy next time).
          if (res && res.ok && res.type !== "opaqueredirect") {
            const ct = res.headers.get("content-type") || "";
            const looksHtml = ct.includes("text/html");
            const wantsHtml = isNavigation;
            if (wantsHtml === looksHtml) {
              const clone = res.clone();
              caches.open(CACHE_VERSION).then((c) => c.put(req, clone));
            }
          }
          return res;
        })
        .catch(() =>
          caches.match(req).then(
            (cached) => cached || caches.match("/ui/"),
          ),
        ),
    );
    return;
  }

  // Other static assets (images / fonts / manifest) → cache-first SWR.
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
