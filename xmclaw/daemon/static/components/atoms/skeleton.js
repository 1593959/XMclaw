// XMclaw — Skeleton atom (Iteration 6)
//
// Pulsing placeholder for loading states. Replaces bare "加载中…" text
// with a shimmer block that hints at the eventual layout.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

export function Skeleton({ lines = 3, height = "1em", width = "100%" }) {
  return html`
    <div style="display:flex;flex-direction:column;gap:.5rem;animation:xmc-skel-pulse 1.5s ease-in-out infinite" aria-hidden="true">
      ${Array.from({ length: lines }, (_, i) => html`
        <div key=${i} style="height:${height};width:${i === lines - 1 ? "60%" : width};background:var(--xmc-border);border-radius:4px;opacity:.35"></div>
      `)}
    </div>
  `;
}
