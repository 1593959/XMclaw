// XMclaw — WebSocket client
//
// Wraps the daemon WS endpoint at /agent/v2/{session_id}, with:
//   * pairing-token query param (?token=<hex>) when present
//   * auto-reconnect with exponential backoff capped at 30s
//   * frame parsing (JSON-only; bad frames logged but not fatal)
//   * pluggable dispatch — connect(...) takes an `onEvent(event)` callback
//     that receives the parsed BehavioralEvent envelope
//
// The connect() return value is a tiny handle:
//     { send(payload), close(), getStatus() }
// Repeat connects on the same handle are idempotent: calling .close() then
// .connect() reopens fresh. The handle owns at most one live socket at a
// time and one pending reconnect timer.
//
// We deliberately avoid an EventEmitter — Phase 1 has exactly one consumer
// (the chat reducer in app.js). When Phase 4 adds the global event timeline
// we'll fan out at the dispatch level, not here.

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 30_000;
const RECONNECT_JITTER = 0.2;

function withJitter(ms) {
  const span = ms * RECONNECT_JITTER;
  return ms + (Math.random() * 2 - 1) * span;
}

function backoff(attempt) {
  const raw = Math.min(RECONNECT_BASE_MS * Math.pow(2, attempt), RECONNECT_MAX_MS);
  return Math.max(0, withJitter(raw));
}

export function buildWsUrl(sessionId, token, location = window.location) {
  const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = new URL(
    wsProto + "//" + location.host + "/agent/v2/" + encodeURIComponent(sessionId)
  );
  if (token) url.searchParams.set("token", token);
  return url.toString();
}

export function createWsClient({
  sessionId,
  token,
  onEvent,
  onStatus,
  // Test seam: factory used to create the underlying socket. Defaults to
  // the global WebSocket. The static_scaffold + ws unit tests inject a
  // fake to avoid real network IO.
  socketFactory = (url) => new WebSocket(url),
  // Test seam: maximum reconnect attempts. Default Infinity for prod;
  // tests pin it to 0 so a connect failure surfaces immediately as
  // "auth_failed" or "disconnected" without scheduling a retry.
  maxReconnects = Infinity,
}) {
  let socket = null;
  let reconnectTimer = null;
  let reconnectAttempt = 0;
  let closedByUser = false;
  let status = "disconnected";

  function setStatus(next, error) {
    if (next === status && !error) return;
    status = next;
    if (onStatus) {
      try {
        onStatus({ status: next, error: error || null, attempt: reconnectAttempt });
      } catch (err) {
        console.error("[xmc/ws] onStatus threw", err);
      }
    }
  }

  function open() {
    if (closedByUser) return;
    setStatus("connecting");
    const url = buildWsUrl(sessionId, token);
    let s;
    try {
      s = socketFactory(url);
    } catch (err) {
      setStatus("disconnected", String(err));
      scheduleReconnect();
      return;
    }
    socket = s;

    s.addEventListener("open", () => {
      reconnectAttempt = 0;
      setStatus("connected");
    });

    s.addEventListener("message", (msg) => {
      let envelope;
      try {
        envelope = JSON.parse(msg.data);
      } catch (err) {
        console.error("[xmc/ws] bad frame", err, msg.data);
        return;
      }
      if (onEvent) {
        try {
          onEvent(envelope);
        } catch (err) {
          console.error("[xmc/ws] onEvent threw", err, envelope);
        }
      }
    });

    s.addEventListener("error", () => {
      // The browser's "error" event is intentionally opaque. We don't
      // surface it as a hard failure — `close` will fire with the real
      // code right after.
    });

    s.addEventListener("close", (evt) => {
      socket = null;
      // Code 4401 = pairing-token rejected. Stop retrying — a fresh token
      // requires a page reload (the user has likely rotated it).
      if (evt && (evt.code === 4401 || evt.code === 4403)) {
        setStatus("auth_failed", "pairing token rejected (code " + evt.code + ")");
        return;
      }
      if (closedByUser) {
        setStatus("disconnected");
        return;
      }
      scheduleReconnect();
    });
  }

  function scheduleReconnect() {
    if (closedByUser) return;
    if (reconnectAttempt >= maxReconnects) {
      setStatus("disconnected", "max reconnect attempts reached");
      return;
    }
    const delay = backoff(reconnectAttempt);
    reconnectAttempt += 1;
    setStatus("reconnecting");
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      open();
    }, delay);
  }

  function send(payload) {
    if (!socket || socket.readyState !== 1 /* OPEN */) {
      console.warn("[xmc/ws] dropped frame; socket not open", payload);
      return false;
    }
    try {
      socket.send(JSON.stringify(payload));
      return true;
    } catch (err) {
      console.error("[xmc/ws] send threw", err);
      return false;
    }
  }

  function close() {
    closedByUser = true;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (socket) {
      try {
        socket.close();
      } catch (_) {
        /* ignore */
      }
      socket = null;
    }
    setStatus("disconnected");
  }

  // Kick off the first connection synchronously so callers get a status
  // transition immediately.
  open();

  return {
    send,
    close,
    getStatus: () => status,
  };
}
