// XMclaw — Memory page: Auto-Dream status card (B-323 follow-up).
//
// Renders MEMORY.md compaction state + collapsible backups list.
// Parent owns the dream snapshot, dreamRunning flag, showBackups
// toggle, backups list (loaded lazily on toggle), and the three
// async handlers (onDreamNow / onToggleBackups / onRestore).

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);


export function AutoDreamCard({
  dream,
  dreamRunning, onDreamNow,
  showBackups, onToggleBackups,
  backups, onRestore,
}) {
  if (!dream) return null;
  return html`
    <div class="xmc-h-card" style="padding:.6rem .8rem;margin:.6rem 0;background:var(--color-bg);border-left:3px solid var(--color-primary, #6aa3f0)">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem">
        <strong style="font-size:.85rem">Auto-Dream 压缩（B-51）</strong>
        ${dream.wired
          ? html`<button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${onDreamNow} disabled=${dreamRunning} style="font-size:.7rem">
              ${dreamRunning ? '运行中…' : '立刻运行'}
            </button>`
          : null}
      </div>
      ${dream.wired
        ? html`
            <div style="margin-top:.3rem;display:flex;gap:.6rem;flex-wrap:wrap;font-size:.8rem">
              <span class="xmc-h-badge xmc-h-badge--${dream.running ? 'success' : 'warn'}">
                ${dream.running ? '运行中' : '未运行'}
              </span>
              <span class="xmc-datapage__subtitle">每日 <strong>${String(dream.hour).padStart(2, '0')}:${String(dream.minute).padStart(2, '0')}</strong></span>
              ${dream.last_run_at
                ? html`<span class="xmc-datapage__subtitle">最近一次: <strong>${new Date(dream.last_run_at * 1000).toLocaleString('zh-CN')}</strong></span>`
                : html`<span class="xmc-datapage__subtitle">尚未运行过</span>`}
              ${dream.last_result && dream.last_result.ok
                ? html`<span class="xmc-h-badge xmc-h-badge--success">节省 ${dream.last_result.saved_chars}</span>`
                : null}
              ${dream.last_result && !dream.last_result.ok
                ? html`<span class="xmc-h-badge xmc-h-badge--error" title=${dream.last_result.error || ''}>上次失败</span>`
                : null}
              <button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${onToggleBackups} style="font-size:.7rem;margin-left:auto">
                ${showBackups ? '隐藏' : '显示'}备份 ${backups != null ? `(${backups.length})` : ''}
              </button>
            </div>
            ${showBackups
              ? html`
                  <div style="margin-top:.4rem;padding:.4rem .6rem;background:var(--color-card);border:1px solid var(--color-border);border-radius:4px;max-height:240px;overflow-y:auto">
                    ${backups == null
                      ? html`<small class="xmc-datapage__subtitle">加载中…</small>`
                      : backups.length === 0
                        ? html`<small class="xmc-datapage__subtitle">尚无备份</small>`
                        : html`
                            <ul class="xmc-datapage__list" style="margin:0">
                              ${backups.map((b) => html`
                                <li class="xmc-datapage__row" key=${b.name} style="display:flex;justify-content:space-between;align-items:center;gap:.5rem;padding:.25rem 0;font-size:.75rem">
                                  <span style="display:flex;flex-direction:column;gap:.1rem;min-width:0;flex:1 1 auto">
                                    <code style="font-size:.7rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${b.name}</code>
                                    <small class="xmc-datapage__subtitle">${b.size}B · ${new Date(b.mtime * 1000).toLocaleString('zh-CN')}</small>
                                  </span>
                                  <button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${() => onRestore(b.name)} style="font-size:.7rem;flex:0 0 auto">还原</button>
                                </li>
                              `)}
                            </ul>
                          `}
                  </div>
                `
              : null}
          `
        : html`
            <div style="margin-top:.3rem;color:var(--xmc-fg-muted);font-size:.78rem">
              ⚠ ${dream.reason || '未启用'}（需配置 LLM）
            </div>
          `}
    </div>
  `;
}
