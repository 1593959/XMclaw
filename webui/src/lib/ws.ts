// Mission Control — WS client。语义 1:1 移植自旧 static/lib/ws.js：
// 指数退避重连（封顶 30s + 抖动）、断线排队 + open 时按序冲洗（B-13）、
// 4401/4403 停止重试、4408 tab 被顶替停止重试、send 失败回队。
// WS 协议零改动。

import type { Envelope, ConnectionStatus } from "./types";

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 30_000;
const RECONNECT_JITTER = 0.2;
// 2026-06-15: if no frame (business event or app-level ping) arrives for
// 45s, assume the connection is dead and force a reconnect.
const NO_FRAME_TIMEOUT_MS = 45_000;

function backoff(attempt: number): number {
  const raw = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS);
  const span = raw * RECONNECT_JITTER;
  return Math.max(0, raw + (Math.random() * 2 - 1) * span);
}

export function buildWsUrl(
  sessionId: string,
  token: string | null,
  loc: { protocol: string; host: string } = window.location,
  agentId: string | null = null,
): string {
  const wsProto = loc.protocol === "https:" ? "wss:" : "ws:";
  const url = new URL(`${wsProto}//${loc.host}/agent/v2/${encodeURIComponent(sessionId)}`);
  if (token) url.searchParams.set("token", token);
  if (agentId && agentId !== "main") url.searchParams.set("agent_id", agentId);
  return url.toString();
}

export interface WsStatusUpdate {
  status: ConnectionStatus;
  error: string | null;
  attempt: number;
}

export interface WsHandle {
  send(payload: unknown): { ok: boolean; queued: boolean; pendingCount?: number; reason?: string };
  close(): void;
  getStatus(): ConnectionStatus;
  getPendingCount(): number;
  consumeLastFlushCount(): number;
}

export interface WsClientOptions {
  sessionId: string;
  token: string | null;
  agentId?: string | null;
  onEvent: (envelope: Envelope) => void;
  onStatus?: (update: WsStatusUpdate) => void;
  socketFactory?: (url: string) => WebSocket;
  maxReconnects?: number;
  pendingQueueMax?: number;
}

export function createWsClient({
  sessionId,
  token,
  agentId = null,
  onEvent,
  onStatus,
  socketFactory = (url) => new WebSocket(url),
  maxReconnects = Infinity,
  pendingQueueMax = 64,
}: WsClientOptions): WsHandle {
  let socket: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectAttempt = 0;
  let closedByUser = false;
  let status: ConnectionStatus = "disconnected";
  const pendingQueue: unknown[] = [];
  let lastFlushCount = 0;
  let frameWatchdog: ReturnType<typeof setTimeout> | null = null;

  function resetFrameWatchdog() {
    if (frameWatchdog) clearTimeout(frameWatchdog);
    frameWatchdog = setTimeout(() => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        console.warn("[mc/ws] no frame for 45s, reconnecting");
        try {
          socket.close();
        } catch {
          /* ignore */
        }
      }
    }, NO_FRAME_TIMEOUT_MS);
  }

  function setStatus(next: ConnectionStatus, error?: string) {
    if (next === status && !error) return;
    status = next;
    try {
      onStatus?.({ status: next, error: error ?? null, attempt: reconnectAttempt });
    } catch (err) {
      console.error("[mc/ws] onStatus threw", err);
    }
  }

  function open() {
    if (closedByUser) return;
    setStatus("connecting");
    let s: WebSocket;
    try {
      s = socketFactory(buildWsUrl(sessionId, token, window.location, agentId));
    } catch (err) {
      setStatus("disconnected", String(err));
      scheduleReconnect();
      return;
    }
    socket = s;

    s.addEventListener("open", () => {
      reconnectAttempt = 0;
      // 先冲洗排队帧再宣布 connected，让状态回调看到排空后的队列。
      let flushed = 0;
      if (pendingQueue.length) {
        const drain = pendingQueue.splice(0);
        for (const payload of drain) {
          try {
            s.send(JSON.stringify(payload));
            flushed += 1;
          } catch (err) {
            console.error("[mc/ws] flush threw, re-queueing", err);
            pendingQueue.unshift(payload);
            break;
          }
        }
      }
      lastFlushCount = flushed;
      resetFrameWatchdog();
      setStatus("connected");
    });

    s.addEventListener("message", (msg) => {
      resetFrameWatchdog();
      let envelope: Envelope;
      try {
        envelope = JSON.parse(msg.data as string);
      } catch (err) {
        console.error("[mc/ws] bad frame", err, msg.data);
        return;
      }
      // 2026-06-15: reply to application-level ping with pong; don't pass
      // it up to the chat reducer.
      if (envelope.type === "ping") {
        send({ type: "pong", payload: {} });
        return;
      }
      // 2026-06-19: skip server-replayed history frames. The frontend already
      // hydrates history via REST (`hydrateHistory` in store/app.ts) on
      // session switch / reconnect. Replayed WS frames share the same
      // content but carry different ids, so without this guard they
      // duplicate every message in the chat transcript.
      if (envelope.replayed === true) {
        return;
      }
      try {
        onEvent(envelope);
      } catch (err) {
        console.error("[mc/ws] onEvent threw", err, envelope);
      }
    });

    s.addEventListener("close", (evt) => {
      socket = null;
      if (evt && (evt.code === 4401 || evt.code === 4403)) {
        setStatus("auth_failed", `pairing token rejected (code ${evt.code})`);
        return;
      }
      // 4408 = 同 session 被另一个 tab 顶替；重连只会 ping-pong，停。
      if (evt && evt.code === 4408) {
        setStatus("superseded", "another tab opened the same session (code 4408)");
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

  function send(payload: unknown) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      try {
        socket.send(JSON.stringify(payload));
        return { ok: true, queued: false };
      } catch (err) {
        console.error("[mc/ws] send threw, re-queueing", err);
      }
    }
    if (closedByUser) return { ok: false, queued: false, reason: "closed_by_user" };
    if (pendingQueue.length >= pendingQueueMax) pendingQueue.shift();
    pendingQueue.push(payload);
    return {
      ok: true,
      queued: true,
      pendingCount: pendingQueue.length,
      reason: status === "connecting" || status === "reconnecting" ? "reconnecting" : "disconnected",
    };
  }

  function close() {
    closedByUser = true;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (frameWatchdog) {
      clearTimeout(frameWatchdog);
      frameWatchdog = null;
    }
    if (socket) {
      try {
        socket.close();
      } catch {
        /* ignore */
      }
      socket = null;
    }
    setStatus("disconnected");
  }

  open();

  return {
    send,
    close,
    getStatus: () => status,
    getPendingCount: () => pendingQueue.length,
    consumeLastFlushCount: () => {
      const n = lastFlushCount;
      lastFlushCount = 0;
      return n;
    },
  };
}
