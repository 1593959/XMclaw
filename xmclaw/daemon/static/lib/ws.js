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

export function buildWsUrl(sessionId, token, location = window.location, agentId = null) {
  const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = new URL(
    wsProto + "//" + location.host + "/agent/v2/" + encodeURIComponent(sessionId)
  );
  if (token) url.searchParams.set("token", token);
  // B-133: route the WS to a specific sub-agent when the user picked one.
  // Daemon falls back to ``main`` when the param is missing or "main".
  if (agentId && agentId !== "main") {
    url.searchParams.set("agent_id", agentId);
  }
  return url.toString();
}

export function createWsClient({
  sessionId,
  token,
  agentId,  // B-133: optional sub-agent id; null/undefined → primary 'main'
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
  // How many frames to retain in the pending-send queue while the
  // socket is closed. When the queue fills, the oldest frame is
  // dropped — capped so a multi-day disconnect doesn't grow without
  // bound. ``Infinity`` = no cap (used by tests).
  pendingQueueMax = 64,
}) {
  let socket = null;
  let reconnectTimer = null;
  let reconnectAttempt = 0;
  let closedByUser = false;
  let status = "disconnected";
  // Frames that arrived while the socket was reconnecting. Flushed
  // in order on the next ``open`` event. Without this, a user who
  // pressed Enter during a reconnect would silently lose their
  // message — the server never received it but the UI showed an
  // optimistic bubble. See B-13 incident.
  const pendingQueue = [];
  // How many frames the most recent ``open`` event drained from the
  // queue. Surfaced via ``getLastFlushCount()`` so the UI can show
  // "已重连 — N 条排队消息已发送" once after a reconnect.
  let lastFlushCount = 0;

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
    const url = buildWsUrl(sessionId, token, window.location, agentId);
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
      // Flush any frames that the user enqueued while we were
      // reconnecting BEFORE announcing "connected". This way the
      // status callback always sees the queue at its post-drain
      // state and can compare delta correctly.
      let flushed = 0;
      if (pendingQueue.length) {
        const drain = pendingQueue.splice(0);
        for (const payload of drain) {
          try {
            s.send(JSON.stringify(payload));
            flushed += 1;
          } catch (err) {
            console.error("[xmc/ws] flush threw, re-queueing", err);
            pendingQueue.unshift(payload);
            break;
          }
        }
      }
      lastFlushCount = flushed;
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
    if (socket && socket.readyState === 1 /* OPEN */) {
      try {
        socket.send(JSON.stringify(payload));
        return { ok: true, queued: false };
      } catch (err) {
        console.error("[xmc/ws] send threw, re-queueing", err);
        // Fall through to the queue path so the frame survives.
      }
    }
    if (closedByUser) {
      // Caller explicitly closed — don't pretend we'll retry.
      return { ok: false, queued: false, reason: "closed_by_user" };
    }
    if (pendingQueue.length >= pendingQueueMax) {
      // Drop the oldest frame to keep the queue bounded. This is the
      // "I left my browser open over the weekend" path; preferring
      // newer messages over older keeps recent intent intact.
      pendingQueue.shift();
    }
    pendingQueue.push(payload);
    return {
      ok: true,
      queued: true,
      pendingCount: pendingQueue.length,
      reason: status === "connecting" || status === "reconnecting"
        ? "reconnecting"
        : "disconnected",
    };
  }

  function getPendingCount() {
    return pendingQueue.length;
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
    getPendingCount,
    // Returns the number of frames flushed by the most recent
    // ``open`` event. Caller is expected to read-and-clear via
    // consumeLastFlushCount().
    consumeLastFlushCount: () => {
      const n = lastFlushCount;
      lastFlushCount = 0;
      return n;
    },
  };
}
