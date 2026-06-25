// Mission Control — fetch 助手 + pairing token。移植自旧 lib/api.js
// + lib/auth.js：token 未就绪短路（B-214）、2s 在飞去重（B-221）、
// pair 端点带退避重试（daemon 重启窗口）。

export class TokenNotReadyError extends Error {
  tokenNotReady = true;
  constructor() {
    super("pairing token not ready yet");
    this.name = "TokenNotReadyError";
  }
}

export function authHeaders(
  token: string | null,
  extra: Record<string, string> = {},
): Record<string, string> {
  return token ? { ...extra, "X-XMC-Token": token } : { ...extra };
}

interface InflightEntry {
  promise: Promise<unknown>;
  until: number;
}
const inflight = new Map<string, InflightEntry>();
const INFLIGHT_TTL_MS = 2000;

export interface ApiError extends Error {
  status?: number;
  body?: unknown;
}

export async function apiGet<T = unknown>(path: string, token: string | null): Promise<T> {
  if (!token) throw new TokenNotReadyError();
  const key = `${path}::${token}`;
  const now = Date.now();
  const entry = inflight.get(key);
  if (entry && entry.until > now) return entry.promise as Promise<T>;
  const promise = (async () => {
    const res = await fetch(path, { headers: authHeaders(token) });
    if (!res.ok) {
      let detail = "";
      let body: unknown = null;
      try {
        body = await res.json();
        const b = body as Record<string, string>;
        detail = b.detail || b.error || "";
      } catch {
        /* ignore */
      }
      const err: ApiError = new Error(
        `${res.status} ${res.statusText}${detail ? `: ${detail}` : ""}`,
      );
      err.status = res.status;
      err.body = body;
      throw err;
    }
    return res.json();
  })();
  inflight.set(key, { promise, until: now + INFLIGHT_TTL_MS });
  promise.finally(() => {
    const e = inflight.get(key);
    if (e && e.promise === promise) e.until = Date.now() + INFLIGHT_TTL_MS;
  });
  return promise as Promise<T>;
}

export async function apiGetFresh<T = unknown>(
  path: string,
  token: string | null,
  signal?: AbortSignal,
): Promise<T> {
  if (!token) throw new TokenNotReadyError();
  const res = await fetch(path, { headers: authHeaders(token), signal });
  if (!res.ok) {
    let detail = "";
    let body: unknown = null;
    try {
      body = await res.json();
      const b = body as Record<string, string>;
      detail = b.detail || b.error || "";
    } catch {
      /* ignore */
    }
    const err: ApiError = new Error(
      `${res.status} ${res.statusText}${detail ? `: ${detail}` : ""}`,
    );
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return res.json();
}

export async function apiSend<T = unknown>(
  method: string,
  path: string,
  body: unknown,
  token: string | null,
): Promise<T> {
  if (!token) throw new TokenNotReadyError();
  const res = await fetch(path, {
    method,
    headers: authHeaders(token, { "Content-Type": "application/json" }),
    body: body == null ? undefined : JSON.stringify(body),
  });
  let json: unknown = null;
  try {
    json = await res.json();
  } catch {
    /* allow empty body */
  }
  if (!res.ok) {
    const b = json as Record<string, string> | null;
    const detail = b && (b.detail || b.error);
    throw new Error(`${res.status} ${res.statusText}${detail ? `: ${detail}` : ""}`);
  }
  return json as T;
}

export const apiPost = <T = unknown>(path: string, body: unknown, token: string | null) =>
  apiSend<T>("POST", path, body, token);

export const apiDelete = <T = unknown>(path: string, token: string | null) =>
  apiSend<T>("DELETE", path, null, token);

export const apiPatch = <T = unknown>(path: string, body: unknown, token: string | null) =>
  apiSend<T>("PATCH", path, body, token);

// ── pairing token ────────────────────────────────────────────────

const PAIR_ENDPOINT = "/api/v2/pair";
const RETRY_DELAYS_MS = [0, 250, 500, 1000, 2000, 2000];

export interface PairResult {
  token: string | null;
  fetched: boolean;
  error?: string;
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function fetchPairingToken(): Promise<PairResult> {
  let lastErr: unknown = null;
  for (const delay of RETRY_DELAYS_MS) {
    if (delay > 0) await sleep(delay);
    try {
      const resp = await fetch(PAIR_ENDPOINT, {
        method: "GET",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!resp.ok) throw new Error(`pair endpoint returned ${resp.status}`);
      const data = await resp.json();
      return { token: data?.token ?? null, fetched: true };
    } catch (err) {
      lastErr = err;
    }
  }
  return { token: null, fetched: false, error: String(lastErr ?? "unknown") };
}

// 媒体 URL 补 token（/api/v2/media/* 走 auth 中间件）。
let mediaToken = "";
export function setMediaToken(t: string | null) {
  mediaToken = t || "";
}
export function resolveMediaUrl(u: string): string {
  if (!u || typeof u !== "string") return u;
  if (u.startsWith("data:") || u.startsWith("http")) return u;
  if (!mediaToken) return u;
  const sep = u.includes("?") ? "&" : "?";
  return `${u}${sep}token=${encodeURIComponent(mediaToken)}`;
}
