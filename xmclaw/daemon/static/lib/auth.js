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

export async function fetchPairingToken() {
  try {
    const resp = await fetch(PAIR_ENDPOINT, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!resp.ok) {
      return {
        token: null,
        fetched: false,
        error: `pair endpoint returned ${resp.status}`,
      };
    }
    const data = await resp.json();
    // Endpoint returns { token: "<hex>" | null }. A null token means
    // "auth disabled" — the daemon will accept the WS upgrade either way.
    return { token: data && data.token ? data.token : null, fetched: true };
  } catch (err) {
    return { token: null, fetched: false, error: String(err) };
  }
}
