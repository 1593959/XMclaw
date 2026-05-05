// XMclaw — small fetch helper used by the data-driven settings/doctor/etc.
// pages. Same-origin (the daemon serves both /ui/ and /api/v2/) so we don't
// need CORS plumbing; we only attach the pairing token as a query param to
// match the WebSocket auth surface.

function withToken(url, token) {
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

// B-214: sentinel for "token not ready" — pages mount BEFORE
// fetchPairingToken() resolves, so the first useEffect fire goes
// out with token=undefined. Pre-B-214 the daemon then returned 401
// and the page rendered "401 Unauthorized: missing or invalid
// pairing token", even though the *next* render with the real
// token would have succeeded. Now apiGet/apiSend short-circuit
// with this error class; pages catch it via `err.tokenNotReady`
// and stay in their "loading" state without polluting the UI or
// the daemon log with spurious 401s.
export class TokenNotReadyError extends Error {
  constructor() {
    super("pairing token not ready yet");
    this.name = "TokenNotReadyError";
    this.tokenNotReady = true;
  }
}

// B-221: in-flight de-dup. Multiple callers hitting the same URL
// within ~2 seconds share ONE fetch promise. Real-data audit:
// daemon.log showed 12 GET /api/v2/sessions from a single port in
// rapid succession — ChatSidebar's 5s poll + xmc:sessions:changed
// event-driven reloads + page mount fetches all racing. Result was
// "page loads forever" feel. This cache holds promises (resolved
// or pending) for 2s after they settle, so a burst of duplicate
// callers gets one round-trip.
const _inflight = new Map(); // url -> { promise, until }
const _INFLIGHT_TTL_MS = 2000;

function _cacheKey(path, token) {
  // Token is part of the URL but identity-stable for the session.
  // Including it just keeps things correct if a token rotates mid-page.
  return `${path}::${token || ""}`;
}

export async function apiGet(path, token) {
  if (!token) throw new TokenNotReadyError();
  const key = _cacheKey(path, token);
  const now = Date.now();
  const entry = _inflight.get(key);
  if (entry && entry.until > now) {
    return entry.promise;
  }
  const promise = (async () => {
    const res = await fetch(withToken(path, token));
    if (!res.ok) {
      let detail = "";
      try {
        const j = await res.json();
        detail = j.detail || j.error || "";
      } catch (_) {
        /* ignore */
      }
      throw new Error(`${res.status} ${res.statusText}${detail ? `: ${detail}` : ""}`);
    }
    return res.json();
  })();
  _inflight.set(key, { promise, until: now + _INFLIGHT_TTL_MS });
  // Always extend cache window once the fetch settles (success or fail)
  // so the very next caller still gets the cached value.
  promise.finally(() => {
    const e = _inflight.get(key);
    if (e && e.promise === promise) {
      e.until = Date.now() + _INFLIGHT_TTL_MS;
    }
  });
  return promise;
}

export async function apiSend(method, path, body, token) {
  if (!token) throw new TokenNotReadyError();
  const res = await fetch(withToken(path, token), {
    method,
    headers: { "Content-Type": "application/json" },
    body: body == null ? undefined : JSON.stringify(body),
  });
  let json = null;
  try {
    json = await res.json();
  } catch (_) {
    /* allow empty body */
  }
  if (!res.ok) {
    const detail = json && (json.detail || json.error);
    throw new Error(`${res.status} ${res.statusText}${detail ? `: ${detail}` : ""}`);
  }
  return json;
}

export const apiPost = (path, body, token) => apiSend("POST", path, body, token);
export const apiPut = (path, body, token) => apiSend("PUT", path, body, token);
export const apiDelete = (path, token) => apiSend("DELETE", path, null, token);
