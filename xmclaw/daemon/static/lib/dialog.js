// XMclaw — in-app confirm dialog (B-46)
//
// Replaces the browser's native ``window.confirm()`` in cron / memory /
// model-profiles / workspace pages. The native dialog (the one
// labelled "127.0.0.1:8765 显示 …") is OS-themed (light backdrop on
// dark XMclaw shell) and gives no styling control. This module
// provides a Promise-based ``confirmDialog({...})`` that renders a
// themed in-page modal matching the rest of the Hermes-style chrome.
//
// Usage:
//
//     import { confirmDialog } from "../lib/dialog.js";
//     const ok = await confirmDialog({
//       title: "删除会话?",
//       body: "对话历史会一同清除，操作不可撤销。",
//       confirmLabel: "删除",
//       confirmTone: "danger",
//     });
//     if (!ok) return;
//
// Same singleton pub/sub shape as ``lib/toast.js`` so a single
// ``<DialogViewport />`` mounted at app root handles every call.

const { h } = window.__xmc.preact;
const { useEffect, useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

const _listeners = new Set();
let _open = null;   // {id, opts, resolve}
let _seq = 0;

function _publish() {
  for (const fn of _listeners) {
    try { fn(_open); } catch (_) { /* ignore */ }
  }
}

export function confirmDialog(opts = {}) {
  // If something's already open, queue behind it — close it first
  // so we don't stack modals (rare but possible if two routes
  // racing).
  if (_open && _open.resolve) {
    _open.resolve(false);
  }
  return new Promise((resolve) => {
    _open = {
      id: `d${++_seq}`,
      opts: {
        title: opts.title || "确认",
        body: opts.body || "",
        confirmLabel: opts.confirmLabel || "确定",
        cancelLabel: opts.cancelLabel || "取消",
        confirmTone: opts.confirmTone || "primary", // primary | danger
      },
      resolve,
    };
    _publish();
  });
}

function _close(value) {
  if (!_open) return;
  const { resolve } = _open;
  _open = null;
  _publish();
  try { resolve(value); } catch (_) { /* ignore */ }
}

// Hard ESC handler so a stuck dialog never traps the user. Mounted
// once by the viewport, removed when the viewport unmounts.
function _useEscToCancel(active) {
  useEffect(() => {
    if (!active) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        _close(false);
      } else if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        _close(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active]);
}

export function DialogViewport() {
  const [state, setState] = useState(_open);
  useEffect(() => {
    _listeners.add(setState);
    return () => _listeners.delete(setState);
  }, []);
  _useEscToCancel(!!state);
  if (!state) return null;
  const { opts } = state;
  const confirmCls = "xmc-h-btn xmc-h-btn--" + (opts.confirmTone === "danger" ? "danger" : "primary");
  return html`
    <div
      class="xmc-h-dialog__backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby=${state.id + "-title"}
      onClick=${(e) => { if (e.target === e.currentTarget) _close(false); }}
      style="position:fixed;inset:0;z-index:9000;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.55);backdrop-filter:blur(2px)"
    >
      <div
        class="xmc-h-dialog xmc-h-dialog--confirm"
        style="background:var(--color-bg-elevated, #0c1c1c);border:1px solid var(--color-border, #1a3a3a);box-shadow:0 12px 40px rgba(0,0,0,0.6);min-width:320px;max-width:520px;padding:1.1rem 1.3rem;border-radius:6px;font-family:var(--xmc-font-mono);color:var(--xmc-fg)"
      >
        <h3
          id=${state.id + "-title"}
          style="margin:0 0 .5rem;font-size:1rem;letter-spacing:.04em;text-transform:uppercase;color:var(--xmc-fg-muted)"
        >
          ${opts.title}
        </h3>
        ${opts.body
          ? html`<p style="margin:0 0 1rem;font-size:.9rem;line-height:1.55;white-space:pre-wrap;color:var(--xmc-fg)">${opts.body}</p>`
          : null}
        <div style="display:flex;gap:.5rem;justify-content:flex-end">
          <button
            type="button"
            class="xmc-h-btn xmc-h-btn--ghost"
            onClick=${() => _close(false)}
          >
            ${opts.cancelLabel}
          </button>
          <button
            type="button"
            class=${confirmCls}
            autofocus
            onClick=${() => _close(true)}
          >
            ${opts.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  `;
}
