// XMclaw — pairing-token fetcher
//
// The daemon mints a 256-bit pairing token at first start (see
// xmclaw/daemon/pairing.py) and exposes it at GET /api/v2/pair when the
// caller is same-origin. The Web UI is served by the same FastAPI app, so
// the browser is automatically same-origin and can read the token.
//
// Returns:
//   { token: string|null, fetched: true }   on success or "no token"
//   { token: null, fetched: false, error }  on network failure
//
// The store keeps the token cached for the lifetime of the page. We don't
// re-fetch on reconnect; if the daemon rotated the token, the WS upgrade
// will fail with 4401 and the UI will surface "auth_failed" — at which
// point the user reloads the tab.

const PAIR_ENDPOINT = "/api/v2/pair";

// B-214: retry the pair fetch a few times. The daemon restarts often
// during dev (config reloads, hot fixes) — without retry, the browser
// catches the daemon mid-restart, gets ECONNREFUSED, and then ALL
// downstream pages stay forever in "loading" state because they think
// auth fetch failed. Caps total wait at ~6s before giving up.
const RETRY_DELAYS_MS = [0, 250, 500, 1000, 2000, 2000];

async function _fetchOnce() {
  const resp = await fetch(PAIR_ENDPOINT, {
    method: "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  if (!resp.ok) {
    throw new Error(`pair endpoint returned ${resp.status}`);
  }
  const data = await resp.json();
  return { token: data && data.token ? data.token : null, fetched: true };
}

function _sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

export async function fetchPairingToken() {
  let lastErr = null;
  for (const delay of RETRY_DELAYS_MS) {
    if (delay > 0) await _sleep(delay);
    try {
      return await _fetchOnce();
    } catch (err) {
      lastErr = err;
    }
  }
  return { token: null, fetched: false, error: String(lastErr || "unknown") };
}
