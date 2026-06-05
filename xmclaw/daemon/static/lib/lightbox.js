// XMclaw — in-app image lightbox
//
// Replaces ``<a target="_blank">`` click-to-enlarge across the chat:
// tool screenshots, markdown ``![](url)`` images, user composer
// uploads. Clicking a thumbnail opens a fullscreen overlay with the
// full-size image centred on a dim backdrop. Click outside or press
// Esc to close — no navigation, no tab switching, no losing chat
// scroll position. Matches lib/dialog.js / lib/toast.js singleton
// pub/sub shape so one ``<LightboxViewport />`` at app root handles
// every call.
//
// Usage:
//
//     import { openLightbox } from "../lib/lightbox.js";
//     // imperative
//     onClick=${() => openLightbox(src, { alt: "screenshot" })}
//
// Or wire a list as a slideshow:
//
//     openLightbox(images[2], { alt, items: images, index: 2 });

const { h } = window.__xmc.preact;
const { useEffect, useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

const _listeners = new Set();
let _open = null;  // { id, src, alt, items?, index? }
let _seq = 0;

function _publish() {
  for (const fn of _listeners) {
    try { fn(_open); } catch (_) { /* ignore */ }
  }
}

export function openLightbox(src, opts = {}) {
  if (!src || typeof src !== "string") return;
  _open = {
    id: `lb${++_seq}`,
    src,
    alt: opts.alt || "",
    items: Array.isArray(opts.items) ? opts.items.slice() : null,
    index: typeof opts.index === "number" ? opts.index : 0,
  };
  _publish();
}

export function closeLightbox() {
  _open = null;
  _publish();
}

function _useSubscribe() {
  const [snap, setSnap] = useState(_open);
  useEffect(() => {
    const sub = (s) => setSnap(s);
    _listeners.add(sub);
    return () => { _listeners.delete(sub); };
  }, []);
  return snap;
}

function _useKeyHandler(active) {
  useEffect(() => {
    if (!active) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closeLightbox();
        return;
      }
      if (!_open || !_open.items || _open.items.length < 2) return;
      if (e.key === "ArrowRight") {
        e.preventDefault();
        const next = (_open.index + 1) % _open.items.length;
        _open = { ..._open, index: next, src: _open.items[next] };
        _publish();
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        const prev = (_open.index - 1 + _open.items.length) % _open.items.length;
        _open = { ..._open, index: prev, src: _open.items[prev] };
        _publish();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); };
  }, [active]);
}

export function LightboxViewport() {
  const snap = _useSubscribe();
  _useKeyHandler(!!snap);
  if (!snap) return null;
  const hasMany = snap.items && snap.items.length > 1;
  const go = (delta) => {
    if (!snap.items) return;
    const i = (snap.index + delta + snap.items.length) % snap.items.length;
    _open = { ..._open, index: i, src: snap.items[i] };
    _publish();
  };
  return html`
    <div
      class="nb-lightbox show"
      role="dialog"
      aria-modal="true"
      aria-label="图片预览"
      onClick=${(e) => { if (e.target === e.currentTarget) closeLightbox(); }}
    >
      <button
        type="button"
        class="nb-lightbox__close"
        onClick=${closeLightbox}
        aria-label="关闭"
        title="关闭 (Esc)"
      >×</button>
      ${hasMany ? html`
        <button
          type="button"
          class="nb-lightbox__nav nb-lightbox__nav--prev"
          onClick=${() => go(-1)}
          aria-label="上一张"
          title="上一张 (←)"
        >‹</button>
      ` : null}
      <img
        class="nb-lightbox__img"
        src=${snap.src}
        alt=${snap.alt}
        onClick=${(e) => e.stopPropagation()}
      />
      ${hasMany ? html`
        <button
          type="button"
          class="nb-lightbox__nav nb-lightbox__nav--next"
          onClick=${() => go(1)}
          aria-label="下一张"
          title="下一张 (→)"
        >›</button>
        <div class="nb-lightbox__counter">${snap.index + 1} / ${snap.items.length}</div>
      ` : null}
    </div>
  `;
}
