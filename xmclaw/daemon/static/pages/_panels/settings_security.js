// XMclaw — Settings / Security tab (Iteration 4)
//
// Secrets manager + guardians policy editor.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet, apiPost, apiDelete } from "../../lib/api.js";
import { toast } from "../../lib/toast.js";

export function SecuritySettings({ token }) {
  const [items, setItems] = useState([]);
  const [enc, setEnc] = useState(false);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [loading, setLoading] = useState(true);

  async function load() {
    try {
      const d = await apiGet("/api/v2/secrets", token);
      setItems(d.items || []);
      setEnc(!!d.encryption_available);
    } catch (_) {
      /* fail silent */
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [token]);

  async function onAdd() {
    if (!name.trim() || !value) return;
    try {
      await apiPost("/api/v2/secrets", { name: name.trim(), value }, token);
      toast.success("Secret 已保存");
      setName(""); setValue("");
      load();
    } catch (e) {
      toast.error("保存失败: " + String(e.message || e));
    }
  }

  async function onRemove(n) {
    try {
      await apiDelete(`/api/v2/secrets/${encodeURIComponent(n)}`, token);
      toast.success("Secret 已删除");
      load();
    } catch (e) {
      toast.error("删除失败: " + String(e.message || e));
    }
  }

  return html`
    <fieldset class="xmc-settings__group">
      <legend>Secrets</legend>
      <p class="xmc-settings__hint">安全凭证管理。值永远不会回显到前端。</p>
      ${enc ? html`<${Badge} tone="success">加密存储已启用</${Badge}>` : html`<${Badge} tone="warn">明文存储</${Badge}>`}

      <div style="display:flex;gap:8px;margin:.75rem 0">
        <input value=${name} onInput=${e => setName(e.target.value)} placeholder="名称" style="flex:1;padding:4px 8px;border-radius:4px;border:1px solid var(--color-border);background:transparent;color:inherit" />
        <input type="password" value=${value} onInput=${e => setValue(e.target.value)} placeholder="值" style="flex:2;padding:4px 8px;border-radius:4px;border:1px solid var(--color-border);background:transparent;color:inherit" />
        <button onClick=${onAdd} style="font-size:.75rem;padding:4px 10px">添加</button>
      </div>

      ${loading ? html`<div>加载中…</div>` : items.length === 0
        ? html`<div style="opacity:.6;font-size:.9rem">尚无 secrets</div>`
        : html`<ul class="xmc-datapage__list">
            ${items.map((it) => html`
              <li class="xmc-datapage__row" key=${it.name} style="display:flex;justify-content:space-between;align-items:center;gap:.5rem">
                <code>${it.name}</code>
                ${it.env_override ? html`<span style="font-size:.7rem;color:var(--xmc-warn)">被环境变量覆盖</span>` : null}
                <button onClick=${() => onRemove(it.name)} style="font-size:.7rem;padding:3px 8px">删除</button>
              </li>
            `)}
          </ul>`}
    </fieldset>
  `;
}

function Badge({ tone, children }) {
  const colors = { success: "#2ecc71", warn: "#f39c12", error: "#e74c3c", info: "#3498db" };
  const c = colors[tone] || colors.info;
  return html`<span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:.75rem;background:${c}22;color:${c};border:1px solid ${c}44">${children}</span>`;
}
