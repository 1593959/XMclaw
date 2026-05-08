// XMclaw — Memory page: top-K LLM relevant-files picker config card
// (B-323 follow-up). Parent owns ``picker`` snapshot, ``pickerForm``
// state, ``pickerSaving`` flag, and ``onSavePicker``.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);


export function PickerCard({
  picker,
  pickerForm, setPickerForm,
  pickerSaving, onSavePicker,
}) {
  if (!picker) return null;
  return html`
    <div class="xmc-h-card" style="padding:.6rem .8rem;margin:.6rem 0;background:var(--color-bg);border-left:3px solid var(--color-primary, #6aa3f0)">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem">
        <strong style="font-size:.85rem">LLM 多文件记忆召回（B-93）</strong>
        <span class="xmc-h-badge xmc-h-badge--${picker.runtime.enabled ? 'success' : 'muted'}" style="font-size:.7rem">
          ${picker.runtime.enabled ? '运行中' : '关闭'}
        </span>
      </div>
      <div style="margin-top:.3rem;color:var(--xmc-fg-muted);font-size:.78rem;line-height:1.6">
        每次回合开始让一个子 LLM 从 <code>~/.xmclaw/memory/*.md</code>
        里挑出最相关的 top-K 笔记整篇注入。补 <code>memory_search</code>
        的"段落级向量召回"以"概念级文件召回"。<strong>每回合多一次 LLM 调用</strong>，
        所以默认关闭。
      </div>
      ${picker.restart_pending ? html`
        <div style="margin-top:.4rem;color:var(--color-warning, #c8a86a);font-size:.75rem">
          🔄 配置已修改但未重启 daemon — 当前运行中的设置：enabled=${picker.runtime.enabled}, k=${picker.runtime.k}, max_chars=${picker.runtime.max_chars}
        </div>
      ` : null}
      <div style="margin-top:.6rem;display:grid;grid-template-columns:auto 1fr;gap:.4rem .6rem;align-items:center;font-size:.78rem">
        <label>开启</label>
        <label style="display:flex;align-items:center;gap:.4rem;cursor:pointer">
          <input
            type="checkbox"
            checked=${pickerForm.enabled}
            onChange=${(e) => setPickerForm({ ...pickerForm, enabled: e.target.checked })}
          />
          <span style="color:var(--xmc-fg-muted);font-size:.74rem">勾上后每次对话会多一次 LLM 调用挑相关笔记</span>
        </label>
        <label>top-K</label>
        <input
          type="number"
          class="xmc-h-input"
          min="1" max="20"
          value=${pickerForm.k}
          onInput=${(e) => setPickerForm({ ...pickerForm, k: Number(e.target.value) || 3 })}
        />
        <label>max_chars</label>
        <input
          type="number"
          class="xmc-h-input"
          min="500" max="50000" step="500"
          value=${pickerForm.max_chars}
          onInput=${(e) => setPickerForm({ ...pickerForm, max_chars: Number(e.target.value) || 4000 })}
        />
      </div>
      <div style="margin-top:.5rem;display:flex;gap:.4rem;justify-content:flex-end">
        <button
          type="button"
          class="xmc-h-btn xmc-h-btn--primary"
          style="font-size:.75rem"
          disabled=${pickerSaving}
          onClick=${onSavePicker}
        >${pickerSaving ? '保存中…' : '保存（需重启 daemon）'}</button>
      </div>
    </div>
  `;
}
