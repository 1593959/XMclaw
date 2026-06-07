// XMclaw — in-app confirm dialog (B-46)
//
// Replaces the browser's native ``window.confirm()`` in cron / memory /
// model-profiles / workspace pages. The native dialog (the one
// labelled "127.0.0.1:8766 显示 …") is OS-themed (light backdrop on
// dark XMclaw shell) and gives no styling control. This module
// provides a Promise-based ``confirmDialog({...})`` that renders a
// themed in-page modal matching the rest of the standard chrome.
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
  const confirmCls = [
    "nb-dialog__btn",
    opts.confirmTone === "danger" ? "danger" : "primary",
  ].join(" ");

  return html`
    <div
      class="nb-dialog-overlay show"
      role="dialog"
      aria-modal="true"
      aria-labelledby=${state.id + "-title"}
      onClick=${(e) => { if (e.target === e.currentTarget) _close(false); }}
    >
      <div class="nb-dialog">
        <div class="nb-dialog__header">
          <h3 id=${state.id + "-title"}>${opts.title}</h3>
          ${opts.body
            ? html`<p>${opts.body}</p>`
            : null}
        </div>
        <div class="nb-dialog__footer">
          <button
            type="button"
            class="nb-dialog__btn"
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
