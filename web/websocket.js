/**
 * XMclaw WebSocket Core
 * Provides WebSocket connection management for main_new.js
 */

const WS_URL = 'ws://127.0.0.1:8765/agent/default';
let ws = null;
let _globalHandler = null;
let _onConnect = null;
let _onDisconnect = null;
let _onError = null;
let _reconnectDelay = 1000;
let _maxReconnectDelay = 30000;
let _reconnectTimer = null;
let _intentionalClose = false;
let _queuedMessages = [];
let _isConnected = false;

function wsConnect() {
    if (ws && ws.readyState === WebSocket.OPEN) return;
    if (ws && ws.readyState === WebSocket.CONNECTING) return;
    
    _intentionalClose = false;
    
    try {
        ws = new WebSocket(WS_URL);
    } catch (e) {
        console.error('[WS] Failed to create WebSocket:', e);
        if (_onError) _onError(e);
        scheduleReconnect();
        return;
    }
    
    ws.onopen = () => {
        console.log('[WS] Connected');
        _isConnected = true;
        _reconnectDelay = 1000;
        
        if (_onConnect) _onConnect();
        
        // Flush queued messages
        while (_queuedMessages.length > 0) {
            const msg = _queuedMessages.shift();
            ws.send(msg);
        }
    };
    
    ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            
            // Filter pong messages (keep-alive)
            if (data.type === 'pong') return;
            
            // Delegate to global handler
            if (_globalHandler) {
                _globalHandler(data);
            }
        } catch (e) {
            console.error('[WS] Failed to parse message:', e);
        }
    };
    
    ws.onclose = (event) => {
        console.log('[WS] Disconnected', event.code, event.reason);
        _isConnected = false;
        
        if (!_intentionalClose) {
            if (_onDisconnect) _onDisconnect(_reconnectDelay);
            scheduleReconnect();
        }
    };
    
    ws.onerror = (error) => {
        console.error('[WS] Error:', error);
        if (_onError) _onError(error);
    };
}

function scheduleReconnect() {
    if (_reconnectTimer) clearTimeout(_reconnectTimer);
    
    _reconnectTimer = setTimeout(() => {
        console.log(`[WS] Reconnecting in ${_reconnectDelay}ms...`);
        wsConnect();
        _reconnectDelay = Math.min(_reconnectDelay * 2, _maxReconnectDelay);
    }, _reconnectDelay);
}

function wsSend(payload) {
    if (!payload) return false;
    
    const msg = JSON.stringify(payload);
    
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(msg);
        return true;
    }
    
    // Queue message for later
    _queuedMessages.push(msg);
    console.log('[WS] Message queued (offline), total:', _queuedMessages.length);
    return false;
}

function wsQueuedCount() {
    return _queuedMessages.length;
}

// Global handler registration
function wsSetGlobalHandler(handler) {
    _globalHandler = handler;
}

function wsOnConnect(callback) {
    _onConnect = callback;
}

function wsOnDisconnect(callback) {
    _onDisconnect = callback;
}

function wsOnError(callback) {
    _onError = callback;
}

// Export functions to window
window.wsConnect = wsConnect;
window.wsSend = wsSend;
window.wsQueuedCount = wsQueuedCount;
window.wsSetGlobalHandler = wsSetGlobalHandler;
window.wsOnConnect = wsOnConnect;
window.wsOnDisconnect = wsOnDisconnect;
window.wsOnError = wsOnError;
