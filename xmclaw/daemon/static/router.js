// XMclaw — SPA router
//
// Uses history.pushState so we keep deep URLs (/chat, /skills, /evolution, …)
// in the address bar while avoiding a full reload. The router is tiny on
// purpose — we only need "path → page component" and "bind to store".
//
// API contract:
//   installRouter(store, routes)
//     - registers click + popstate handlers
//     - writes `store.state.route` on every navigation
//     - returns `navigate(path)` for programmatic moves
//
// Route table format (see app.js):
//   { "/chat": ChatPage, "/skills": SkillsPage, ... }
// Unknown paths resolve to routes["*"] if present, else the default "/chat".

const DEFAULT_ROUTE = "/chat";
const BASE_PREFIX = "/ui";

function parsePath(raw) {
  // Strip the /ui mount prefix so the canonical app paths inside the SPA
  // are /chat, /skills, … — not /ui/chat. The daemon serves this bundle
  // from /ui/ (see xmclaw/daemon/app.py).
  let path = raw;
  if (path.startsWith(BASE_PREFIX)) {
    path = path.slice(BASE_PREFIX.length) || "/";
  }
  // Strip trailing slash for canonicalisation; "/" stays as default.
  if (path.length > 1 && path.endsWith("/")) {
    path = path.slice(0, -1);
  }
  return path || DEFAULT_ROUTE;
}

function toUrl(path) {
  if (path.startsWith(BASE_PREFIX)) return path;
  return BASE_PREFIX + (path.startsWith("/") ? path : "/" + path);
}

export function installRouter(store, routes) {
  function resolve(path) {
    if (routes[path]) return { path, component: routes[path] };
    if (routes["*"]) return { path, component: routes["*"] };
    return { path: DEFAULT_ROUTE, component: routes[DEFAULT_ROUTE] };
  }

  function applyLocation() {
    const path = parsePath(window.location.pathname);
    const resolved = resolve(path);
    store.setState({ route: { path: resolved.path, params: {} } });
  }

  function navigate(path, { replace = false } = {}) {
    const target = parsePath(path);
    const url = toUrl(target);
    if (replace) {
      window.history.replaceState({}, "", url);
    } else {
      window.history.pushState({}, "", url);
    }
    applyLocation();
  }

  function onClick(evt) {
    // Intercept plain left-clicks on same-origin <a href="/..."> links.
    // Let modifier clicks / target=_blank go through untouched.
    if (evt.defaultPrevented) return;
    if (evt.button !== 0) return;
    if (evt.metaKey || evt.ctrlKey || evt.shiftKey || evt.altKey) return;

    const anchor = evt.target.closest("a[href]");
    if (!anchor) return;
    if (anchor.target && anchor.target !== "_self") return;

    const href = anchor.getAttribute("href");
    if (!href || href.startsWith("http") || href.startsWith("mailto:")) return;

    evt.preventDefault();
    navigate(href);
  }

  window.addEventListener("popstate", applyLocation);
  document.addEventListener("click", onClick);

  applyLocation(); // initial sync
  return { navigate, resolve };
}
