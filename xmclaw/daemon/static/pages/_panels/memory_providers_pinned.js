// XMclaw — Memory page: pinned-fact CRUD card (B-323 follow-up).
//
// Renders the ## Pinned section editor — add/remove form. Parent
// owns ``pinned`` list, ``pinDraft`` text input, ``pinBusy`` flag,
// and the two async handlers.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);


export function PinnedCard({
  pinned,
  pinDraft, setPinDraft,
  pinBusy, onAddPin, onRemovePin,
}) {
  if (pinned === null) return null;
  return html`
    <div class="xmc-h-card" style="padding:.6rem .8rem;margin:.6rem 0;background:var(--color-bg);border-left:3px solid var(--color-success, #6ac88a)">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem">
        <strong style="font-size:.85rem">📌 Pinned（B-98 永不被 Auto-Dream 压缩）</strong>
        <span class="xmc-h-badge xmc-h-badge--muted" style="font-size:.7rem">
          ${pinned.length} 条
        </span>
      </div>
      <div style="margin-top:.3rem;color:var(--xmc-fg-muted);font-size:.78rem;line-height:1.55">
        ## Pinned 里的 bullet 在 dream 压缩时会被原封不动保留（B-53）。
        Agent 也可以用 <code>memory_pin</code> 工具往这里写。
      </div>
      <div style="margin-top:.5rem;display:flex;gap:.4rem;flex-wrap:wrap">
        <input
          type="text"
          class="xmc-h-input"
          value=${pinDraft}
          placeholder="新 pin 一条（如：永远不要把 .env 提交到 git）"
          onInput=${(e) => setPinDraft(e.target.value)}
          onKeyDown=${(e) => { if (e.key === "Enter" && pinDraft.trim()) onAddPin(); }}
          style="flex:1 1 240px;min-width:0"
        />
        <button
          type="button"
          class="xmc-h-btn xmc-h-btn--primary"
          style="font-size:.75rem"
          onClick=${onAddPin}
          disabled=${pinBusy || !pinDraft.trim()}
        >Pin</button>
      </div>
      ${pinned.length > 0 ? html`
        <ul style="margin:.5rem 0 0;padding:0;list-style:none">
          ${pinned.map((p) => html`
            <li
              key=${p.line}
              style="display:flex;justify-content:space-between;align-items:flex-start;gap:.5rem;padding:.35rem .5rem;border-top:1px dashed rgba(106,200,138,.2)"
            >
              <span style="flex:1;font-size:.78rem;line-height:1.5">${p.text}</span>
              <button
                type="button"
                class="xmc-h-btn xmc-h-btn--ghost"
                style="font-size:.7rem;padding:.1rem .5rem;flex-shrink:0"
                onClick=${() => onRemovePin(p.line)}
                title="取消 pin"
              >×</button>
            </li>
          `)}
        </ul>
      ` : html`
        <p style="margin:.5rem 0 0;font-size:.74rem;color:var(--xmc-fg-muted);font-style:italic">
          暂无 pin 项 — 在上面输入要永久保留的事实后回车。
        </p>
      `}
    </div>
  `;
}
