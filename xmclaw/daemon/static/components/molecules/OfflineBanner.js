// XMclaw — Offline Banner (molecule)
//
// Fixed-top banner that appears when the network connection drops.
// Shows an amber/red gradient background with a reconnect status
// message and a close button.
//
// Props:
//   visible    boolean  – whether the banner is shown
//   message    string   – status text (default: "网络连接已断开")
//   retrying   boolean  – if true, shows a spinner / retrying hint
//   onClose    function – called when the user clicks the close button
//   onRetry    function – called when the user clicks the retry area

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

export function OfflineBanner({
  visible,
  message = "网络连接已断开",
  retrying = false,
  onClose,
  onRetry,
}) {
  if (!visible) return null;

  return html`
    <div class="nb-offline-banner show" role="alert" aria-live="assertive">
      <span aria-hidden="true">⚠</span>
      <span>${message}${retrying ? " · 正在重试…" : ""}</span>
      ${onRetry
        ? html`<button
            type="button"
            style="margin-left:8px;background:transparent;border:1px solid rgba(245,158,11,0.4);color:var(--nb-warning);padding:2px 10px;border-radius:var(--nb-radius-sm);font-size:12px;cursor:pointer;"
            onClick=${onRetry}
          >重试</button>`
        : null}
      <button
        type="button"
        style="margin-left:auto;background:transparent;border:none;color:var(--nb-warning);font-size:16px;cursor:pointer;line-height:1;"
        onClick=${() => onClose?.()}
        aria-label="关闭"
        title="关闭"
      >×</button>
    </div>
  `;
}
