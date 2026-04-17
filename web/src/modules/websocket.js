/** @module websocket - WebSocket connection manager with offline queue and heartbeat */
import { handleEvolutionNotify, handleReflectionComplete } from './notifications.js';

const WS_URL = 'ws://127.0.0.1:8765/agent/default';
const HEARTBEAT_INTERVAL = 25000; // ms
const MAX_RECONNECT_DELAY = 30000;

// ── Connection state ──────────────────────────────────────────────────────────
let ws = null;
let _isConnected = false;
let _reconnectDelay = 2000;
let _heartbeatTimer = null;
const _offlineQueue = [];

// ── Event callbacks (set by other modules) ───────────────────────────────────
/** Registered message-type handlers: type → (data) => void */
const _handlers = {};

/**
 * Inject the complete message renderer from main_new.js.
 * Called once during init. Receives every parsed WS message after
 * built-in processing (pong, evolution:notify) is done.
 */
let _globalHandler = null;
export function setGlobalHandler(fn) { _globalHandler = fn; }

/** Register a message-type handler. Call this during init. */
export function onMessage(type, handler) {
    _handlers[type] = handler;
}

/** Register lifecycle callbacks. */
export function onConnect(fn)      { _lifecycle.onConnect    = fn; }
export function onDisconnect(fn)   { _lifecycle.onDisconnect = fn; }
export function onError(fn)       { _lifecycle.onError     = fn; }

/** Send a message. Returns true if sent, false if queued offline. */
export function send(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(typeof data === 'string' ? data : JSON.stringify(data));
        return true;
    }
    _offlineQueue.push(typeof data === 'string' ? data : JSON.stringify(data));
    return false;
}

/** True if currently connected. */
export function isConnected() { return _isConnected; }

/** Number of queued offline messages. */
export function queuedCount() { return _offlineQueue.length; }

// ── Heartbeat ────────────────────────────────────────────────────────────────
function _startHeartbeat() {
    _stopHeartbeat();
    _heartbeatTimer = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
            try { ws.send(JSON.stringify({ type: 'ping' })); } catch (_) {}
        }
    }, HEARTBEAT_INTERVAL);
}

function _stopHeartbeat() {
    if (_heartbeatTimer !== null) {
        clearInterval(_heartbeatTimer);
        _heartbeatTimer = null;
    }
}

// ── Offline queue replay ──────────────────────────────────────────────────────
function _flushOfflineQueue() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    while (_offlineQueue.length > 0) {
        const msg = _offlineQueue.shift();
        try {
            ws.send(msg);
        } catch (_) {
            _offlineQueue.unshift(msg);
            break;
        }
    }
}

// ── Connect ───────────────────────────────────────────────────────────────────
export function connect() {
    ws = new WebSocket(WS_URL);

    ws.onopen = () => {
        _isConnected = true;
        _reconnectDelay = 2000;
        _startHeartbeat();
        _flushOfflineQueue();
        if (typeof _lifecycle.onConnect === 'function') _lifecycle.onConnect();
    };

    ws.onerror = (e) => {
        _isConnected = false;
        if (typeof _lifecycle.onError === 'function') _lifecycle.onError(e);
    };

    ws.onclose = () => {
        _isConnected = false;
        _stopHeartbeat();
        if (typeof _lifecycle.onDisconnect === 'function') _lifecycle.onDisconnect(_reconnectDelay);
        setTimeout(connect, _reconnectDelay);
        _reconnectDelay = Math.min(_reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
    };

    ws.onmessage = (event) => {
        let data;
        try {
            data = JSON.parse(event.data);
        } catch (_) {
            return;
        }
        if (data.type === 'pong') return;

        // Built-in: evolution:notify from EventBus — show in-dashboard notification
        if (data.type === 'event' && data.event?.event_type === 'evolution:notify') {
            handleEvolutionNotify(data.event.payload || {});
            return;
        }

        // Built-in: reflection:complete from EventBus — render in UI
        if (data.type === 'event' && data.event?.event_type === 'reflection:complete') {
            handleReflectionComplete(data.event.payload || {});
            return;
        }

        // Type-specific handlers (for modular components)
        const typeHandler = _handlers[data.type];
        if (typeHandler) {
            try { typeHandler(data); } catch (e) { console.error('[ws handler error]', e); }
        }

        // Global handler: main_new.js message renderer
        if (_globalHandler) {
            try { _globalHandler(data); } catch (e) { console.error('[global handler error]', e); }
        }
    };
}

// ── Expose on window for non-module callers ────────────────────────────────────
window.wsConnect = connect;
window.wsSend = send;
window.wsSetGlobalHandler = setGlobalHandler;
window.wsOnConnect = onConnect;
window.wsOnDisconnect = onDisconnect;
window.wsOnError = onError;
window.wsIsConnected = isConnected;
window.wsQueuedCount = queuedCount;
