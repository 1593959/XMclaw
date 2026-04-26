// XMclaw — ModelProfiles section
//
// Multi-profile manager. Lets the user deploy several named LLM
// endpoints (e.g. "haiku-fast", "gpt4o-vision", "deepseek-local")
// alongside the legacy default block, then pick which one drives
// each chat session via the chat-header dropdown.
//
// Embedded inside Settings.js (not a top-level route) because the
// "default LLM" form on Settings already owns the LLM concept; a
// separate sidebar entry would split a coherent page.
//
// Backed by GET/POST/DELETE /api/v2/llm/profiles (see
// xmclaw/daemon/routers/llm_profiles.py). The daemon writes to
// daemon/config.json and returns restart_required:true; we surface
// that prominently so the user understands the dropdown won't update
// until the daemon restarts.

const { h } = window.__xmc.preact;
const { useEffect, useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Button } from "../components/atoms/button.js";
import { Badge } from "../components/atoms/badge.js";
import { apiGet, apiPost, apiDelete } from "../lib/api.js";

const PROVIDER_OPTIONS = [
  { value: "anthropic", label: "Anthropic / Claude" },
  { value: "openai",    label: "OpenAI / 兼容 API (DeepSeek / MiniMax / 自托管)" },
];

function emptyForm() {
  return {
    id: "",
    label: "",
    provider: "anthropic",
    model: "",
    base_url: "",
    api_key: "",
    editingExisting: false,
  };
}

export function ModelProfilesSection({ token }) {
  const [profiles, setProfiles] = useState([]);
  const [onDisk, setOnDisk] = useState([]);
  const [defaultId, setDefaultId] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  const [form, setForm] = useState(emptyForm());

  async function reload() {
    try {
      const data = await apiGet("/api/v2/llm/profiles", token);
      setProfiles(data.profiles || []);
      setOnDisk(data.on_disk || []);
      setDefaultId(data.default_id || null);
      setError(null);
    } catch (exc) {
      setError(String(exc.message || exc));
    }
  }

  useEffect(() => { reload(); }, [token]);

  function setField(name, value) {
    setForm((f) => ({ ...f, [name]: value }));
  }

  function startEdit(entry) {
    setForm({
      id: entry.id,
      label: entry.label || "",
      provider: entry.provider,
      model: entry.model,
      base_url: entry.base_url || "",
      api_key: "",  // empty means "keep existing"
      editingExisting: true,
    });
  }

  function startCreate() {
    setForm(emptyForm());
  }

  async function onSave(evt) {
    evt && evt.preventDefault && evt.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body = {
        id: form.id.trim(),
        label: form.label.trim(),
        provider: form.provider,
        model: form.model.trim(),
      };
      if (form.base_url.trim()) body.base_url = form.base_url.trim();
      if (form.api_key) body.api_key = form.api_key;
      else body.api_key = "";  // backend preserves existing on empty string
      const res = await apiPost("/api/v2/llm/profiles", body, token);
      if (!res.ok) throw new Error(res.error || "save failed");
      setSavedAt(Date.now());
      setForm(emptyForm());
      await reload();
    } catch (exc) {
      setError(String(exc.message || exc));
    } finally {
      setBusy(false);
    }
  }

  async function onDelete(id) {
    if (!confirm(`确定删除 profile "${id}"？需重启 daemon 才会生效。`)) return;
    setBusy(true);
    setError(null);
    try {
      const res = await apiDelete(`/api/v2/llm/profiles/${encodeURIComponent(id)}`, token);
      if (!res.ok) throw new Error(res.error || "delete failed");
      await reload();
    } catch (exc) {
      setError(String(exc.message || exc));
    } finally {
      setBusy(false);
    }
  }

  // Merge runtime + on-disk views: an entry can exist on disk but not
  // in the runtime registry (e.g. missing api_key). We show both so the
  // user can spot a misconfigured profile and fix it.
  const runtimeIds = new Set(profiles.map((p) => p.id));
  const brokenOnDisk = onDisk.filter((p) => p.id !== "default" && !runtimeIds.has(p.id));
  const namedRuntime = profiles.filter((p) => p.id !== "default");

  return html`
    <fieldset class="xmc-settings__group">
      <legend>多模型 profiles</legend>
      <p class="xmc-settings__hint">
        除上面"默认"外，可再注册多个命名 profile，会话顶部下拉切换。
        ${defaultId ? html` 当前默认：<code>${defaultId}</code>` : null}
      </p>

      ${error ? html`<${Badge} tone="error">${error}</${Badge}>` : null}
      ${savedAt ? html`<${Badge} tone="success">已保存 — 重启 daemon 后生效</${Badge}>` : null}

      ${namedRuntime.length === 0 && brokenOnDisk.length === 0
        ? html`<p class="xmc-datapage__empty">尚无命名 profile — 用下方表单添加第一个。</p>`
        : html`
          <ul class="xmc-datapage__list">
            ${namedRuntime.map((p) => html`
              <li class="xmc-datapage__row" key=${p.id}>
                <div style="display:flex;justify-content:space-between;gap:.5rem;align-items:center">
                  <div>
                    <strong>${p.label || p.id}</strong>
                    <code>${p.id}</code>
                    <small> · ${p.provider} · ${p.model}</small>
                  </div>
                  <div style="display:flex;gap:.5rem">
                    <button type="button" onClick=${() => startEdit(
                      onDisk.find((d) => d.id === p.id) || p,
                    )} disabled=${busy}>编辑</button>
                    <button type="button" onClick=${() => onDelete(p.id)} disabled=${busy}>删除</button>
                  </div>
                </div>
              </li>
            `)}
            ${brokenOnDisk.map((p) => html`
              <li class="xmc-datapage__row" key=${p.id}>
                <div style="display:flex;justify-content:space-between;gap:.5rem;align-items:center">
                  <div>
                    <strong>${p.label || p.id}</strong>
                    <code>${p.id}</code>
                    <${Badge} tone="warn">未加载</${Badge}>
                    <small> · ${p.provider || "?"} · ${p.model || "?"}</small>
                  </div>
                  <div style="display:flex;gap:.5rem">
                    <button type="button" onClick=${() => startEdit(p)} disabled=${busy}>修复</button>
                    <button type="button" onClick=${() => onDelete(p.id)} disabled=${busy}>删除</button>
                  </div>
                </div>
              </li>
            `)}
          </ul>
        `}

      <form class="xmc-settings__form" onSubmit=${onSave} style="margin-top:.75rem">
        <h4 style="margin:.25rem 0">${form.editingExisting ? `编辑 ${form.id}` : "新增 profile"}</h4>

        <label class="xmc-settings__field">
          <span>ID（标识符，全局唯一）</span>
          <input
            type="text"
            value=${form.id}
            onInput=${(e) => setField("id", e.target.value)}
            placeholder="haiku-fast"
            disabled=${form.editingExisting}
            pattern="[a-z0-9][a-z0-9_-]*"
            required
          />
          <small class="xmc-settings__hint">小写字母 / 数字 / -_，会话切换下拉里用它标识</small>
        </label>

        <label class="xmc-settings__field">
          <span>显示名</span>
          <input
            type="text"
            value=${form.label}
            onInput=${(e) => setField("label", e.target.value)}
            placeholder="Claude Haiku (快速)"
          />
        </label>

        <label class="xmc-settings__field">
          <span>Provider</span>
          <select
            value=${form.provider}
            onChange=${(e) => setField("provider", e.target.value)}
          >
            ${PROVIDER_OPTIONS.map((o) => html`
              <option key=${o.value} value=${o.value}>${o.label}</option>
            `)}
          </select>
        </label>

        <label class="xmc-settings__field">
          <span>模型名</span>
          <input
            type="text"
            value=${form.model}
            onInput=${(e) => setField("model", e.target.value)}
            placeholder="claude-haiku-4-5-20251001 / gpt-4o / deepseek-chat"
            required
          />
        </label>

        <label class="xmc-settings__field">
          <span>Base URL（可选，自托管 / 兼容 API 用）</span>
          <input
            type="text"
            value=${form.base_url}
            onInput=${(e) => setField("base_url", e.target.value)}
            placeholder="留空使用默认"
          />
        </label>

        <label class="xmc-settings__field">
          <span>API Key${form.editingExisting ? html` <small>（留空保留原值）</small>` : null}</span>
          <input
            type="password"
            value=${form.api_key}
            onInput=${(e) => setField("api_key", e.target.value)}
            placeholder=${form.editingExisting ? "留空则保留现有 key" : "粘贴 api_key"}
            autocomplete="off"
          />
        </label>

        <div class="xmc-settings__actions">
          <${Button} type="submit" variant="primary" disabled=${busy || !form.id.trim() || !form.model.trim()}>
            ${busy ? "保存中…" : (form.editingExisting ? "更新" : "添加")}
          </${Button}>
          ${form.editingExisting ? html`
            <button type="button" onClick=${startCreate} disabled=${busy}>取消编辑</button>
          ` : null}
        </div>
      </form>
    </fieldset>
  `;
}
