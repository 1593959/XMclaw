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

export async function apiGet(path, token) {
  if (!token) throw new TokenNotReadyError();
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
