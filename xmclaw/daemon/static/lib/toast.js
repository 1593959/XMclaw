// XMclaw — toast notification system
//
// Direct-port of open-webui's `svelte-sonner` shape into Preact + htm.
// One module-level pub/sub + an in-page <ToastViewport> rendered at app
// root. Anywhere that wants to surface a transient message — connection
// dropped, save failed, copy succeeded — calls `pushToast(level, msg)`.
//
// Design cribbed from `svelte-sonner` (open-webui) but trimmed:
//  - 3 levels: info / success / error
//  - 3 second auto-dismiss (or persistent on hover via dataset flag)
//  - 5-toast cap, oldest pops when 6th arrives (no scroll spam)

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

const _listeners = new Set();
let _toasts = [];
let _seq = 0;

function _publish() {
  for (const fn of _listeners) {
    try { fn(_toasts.slice()); } catch (_) { /* ignore */ }
  }
}

export function pushToast(level, message, opts = {}) {
  const id = `t${++_seq}`;
  const ttl = opts.ttl != null ? opts.ttl : 3000;
  const t = { id, level, message: String(message || ""), ts: Date.now() };
  _toasts = _toasts.concat(t);
  if (_toasts.length > 5) _toasts = _toasts.slice(-5);
  _publish();
  if (ttl > 0) {
    setTimeout(() => dismissToast(id), ttl);
  }
  return id;
}

export function dismissToast(id) {
  const before = _toasts.length;
  _toasts = _toasts.filter((t) => t.id !== id);
  if (_toasts.length !== before) _publish();
}

export function subscribeToasts(fn) {
  _listeners.add(fn);
  fn(_toasts.slice());
  return () => _listeners.delete(fn);
}

// Render-side: drop <ToastViewport /> once at the app root. It hooks
// into the pub/sub above and renders all current toasts in a fixed
// stack at the bottom-right (mirrors open-webui's Sonner positioning).
export function ToastViewport() {
  const [toasts, setToasts] = useState([]);
  useEffect(() => subscribeToasts(setToasts), []);
  if (!toasts.length) return null;
  return html`
    <div class="xmc-toasts" role="status" aria-live="polite" aria-atomic="false">
      ${toasts.map((t) => html`
        <div
          key=${t.id}
          class=${"xmc-toast xmc-toast--" + t.level}
          role="alert"
          onClick=${() => dismissToast(t.id)}
        >
          <span class="xmc-toast__icon" aria-hidden="true">
            ${t.level === "error" ? "✗" : t.level === "success" ? "✓" : "ℹ"}
          </span>
          <span class="xmc-toast__msg">${t.message}</span>
        </div>
      `)}
    </div>
  `;
}

// Convenience helpers — match svelte-sonner's API surface.
export const toast = {
  info:    (m, o) => pushToast("info", m, o),
  success: (m, o) => pushToast("success", m, o),
  error:   (m, o) => pushToast("error", m, o),
};
