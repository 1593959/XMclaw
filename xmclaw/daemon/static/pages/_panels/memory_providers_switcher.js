// XMclaw — Memory page: provider list + external-provider switcher
// + how-to-write-provider help (B-323 follow-up).

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);


export function ProviderListSection({ data }) {
  return html`
    <p class="xmc-datapage__subtitle" style="margin:.6rem 0 1rem">
      XMclaw 的内存层是 Hermes-style 可插拔架构（B-25/B-26 完成）：
      <strong>1 个内置 provider + 至多 1 个外部 provider</strong>。
      外部 provider 优先（active recall），内置 provider 永远在底（fallback）。
    </p>
    <ul class="xmc-datapage__list">
      ${(data.providers || []).map((p) => html`
        <li class="xmc-datapage__row" key=${p.name}>
          <div style="display:flex;justify-content:space-between;align-items:baseline;gap:.5rem;flex-wrap:wrap">
            <strong style="font-size:1rem">${p.name}</strong>
            <span class="xmc-h-badge xmc-h-badge--${p.kind === 'builtin' ? 'success' : 'info'}" style="font-size:.7rem">
              ${p.kind === 'builtin' ? '内置 (永久)' : '外部 (可换)'}
            </span>
          </div>
          <div style="margin-top:.25rem;color:var(--xmc-fg-muted);font-size:.78rem">
            ${p.tool_count > 0
              ? html`暴露 ${p.tool_count} 个 LLM 工具: ${(p.tools || []).slice(0, 3).map((t) => html`<code key=${t} style="margin-right:.3rem">${t}</code>`)}`
              : html`<small>不暴露 LLM 工具</small>`}
          </div>
        </li>
      `)}
    </ul>
  `;
}


export function ProviderSwitcher({ available, selected, busy, onSwitch }) {
  if (!available || available.length === 0) return null;
  const cur = (available || []).find((p) => p.id === selected);
  return html`
    <h3 style="margin:1.2rem 0 .5rem">切换外部 provider</h3>
    <div class="xmc-datapage__row" style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
      <select
        value=${selected}
        onChange=${(e) => onSwitch(e.target.value)}
        disabled=${busy}
        style="padding:.4rem .5rem;font-size:.9rem;min-width:220px"
      >
        ${available.map((p) => html`
          <option value=${p.id} key=${p.id}>${p.label}</option>
        `)}
      </select>
      <small class="xmc-datapage__subtitle">切换需重启 daemon 生效</small>
    </div>
    ${cur ? html`
      <div class="xmc-h-card" style="padding:.5rem .8rem;margin-top:.5rem;background:var(--color-bg)">
        <small style="color:var(--xmc-fg-muted)">${cur.description}</small>
        ${(cur.needs || []).length > 0 ? html`
          <div style="margin-top:.3rem">
            <small style="color:var(--xmc-fg-muted)">需要配置：</small>
            ${cur.needs.map((n) => html`<code key=${n} style="margin-right:.4rem;font-size:.7rem">${n}</code>`)}
          </div>
        ` : null}
      </div>
    ` : null}
  `;
}


export function WriteProviderHelp() {
  return html`
    <h3 style="margin:1.2rem 0 .5rem">如何写一个新 provider</h3>
    <p class="xmc-datapage__subtitle">
      实现 <code>xmclaw/providers/memory/base.MemoryProvider</code> ABC（put / query / forget +
      可选的 prefetch / sync_turn / on_session_end / on_pre_compress / get_tool_schemas /
      handle_tool_call），放到 <code>xmclaw/providers/memory/&lt;name&gt;.py</code>，
      在 <code>factory.py</code> 注册即可 — agent_loop 不需修改。
      参考实现 <code>builtin_file.py</code>（内置）/ <code>sqlite_vec.py</code>（外部）/
      <code>hindsight.py</code>（云 KG 模板）。
    </p>
  `;
}
