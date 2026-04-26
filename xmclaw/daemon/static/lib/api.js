// XMclaw — small fetch helper used by the data-driven settings/doctor/etc.
// pages. Same-origin (the daemon serves both /ui/ and /api/v2/) so we don't
// need CORS plumbing; we only attach the pairing token as a query param to
// match the WebSocket auth surface.

function withToken(url, token) {
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

export async function apiGet(path, token) {
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
