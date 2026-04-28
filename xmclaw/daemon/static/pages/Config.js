// XMclaw — ConfigPage 1:1 layout port of hermes-agent ConfigPage.tsx
//
// Hermes layout (ConfigPage.tsx:75-end):
//   Sticky left filter panel listing top-level config categories with
//   icons, search across all key paths, right scroll panel with
//   AutoField-rendered inputs grouped by category. Form/YAML toggle.
//   Save / Reset / Import / Export buttons.
//
// XMclaw doesn't ship a config schema endpoint, so categories are
// inferred from the config dict's top-level keys and AutoField picks
// the input type from JS typeof. The visual surface — sticky panel,
// search bar, AutoField inputs, dirty-tracking save bar — is 1:1 with
// the Hermes shape; the data shape under it is XMclaw config.json.

const { h } = window.__xmc.preact;
const { useState, useEffect, useMemo, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";

function Icon({ d, className }) {
  return html`
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
         class=${"xmc-icon " + (className || "")} aria-hidden="true">
      <path d=${d} />
    </svg>
  `;
}

const I_SEARCH = "M11 17a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM21 21l-4.3-4.3";
const I_X      = "M18 6 6 18 M6 6l12 12";
const I_SAVE   = "M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2zM17 21v-8H7v8 M7 3v5h8";
const I_RESET  = "M3 7v6h6 M21 17a9 9 0 1 1-3-7l3 3";
const I_FILTER = "M22 3H2l8 9.46V19l4 2v-8.54z";
const I_CODE   = "m16 18 6-6-6-6 M8 6l-6 6 6 6";
const I_FORM   = "M3 5h18 M3 12h18 M3 19h18";

// Category icons (mirrors Hermes CATEGORY_ICONS map).
const CAT_GLYPHS = {
  llm: "🤖",
  evolution: "✨",
  memory: "🧠",
  gateway: "🌐",
  tools: "🔧",
  mcp_servers: "🔌",
  integrations: "🔗",
  security: "🔒",
  workspace: "📁",
  agent: "🤖",
  general: "⚙",
  persona: "👤",
};

// Chinese display labels for the top-level categories. Falls back to
// the raw key when missing, so a config with a custom new section
// still renders rather than crashing on a lookup miss.
const CAT_LABELS = {
  llm: "模型与 API",
  evolution: "技能进化",
  memory: "记忆与向量库",
  gateway: "网关与认证",
  tools: "工具与权限",
  mcp_servers: "MCP 服务器",
  integrations: "外部集成",
  security: "安全策略",
  workspace: "工作区设置",
  agent: "Agent 默认",
  general: "通用设置",
  persona: "人格档案",
};

function catLabel(c) {
  return CAT_LABELS[c] || c;
}

// ── AutoField (port of components/AutoField.tsx) ─────────────────

function AutoField({ value, onChange, label, path, hint }) {
  const id = "cfg-" + path.replace(/[^a-zA-Z0-9]/g, "-");
  if (typeof value === "boolean") {
    return html`
      <div class="xmc-h-cfg__field">
        <label for=${id} class="xmc-h-cfg__label">${label}</label>
        <label class="xmc-h-cfg__switch">
          <input
            id=${id}
            type="checkbox"
            checked=${value}
            onChange=${(e) => onChange(e.target.checked)}
          />
          <span class="xmc-h-cfg__switch-track"></span>
          <span class="xmc-h-cfg__switch-state">${value ? "on" : "off"}</span>
        </label>
        ${hint ? html`<small class="xmc-h-cfg__hint">${hint}</small>` : null}
      </div>
    `;
  }
  if (typeof value === "number") {
    return html`
      <div class="xmc-h-cfg__field">
        <label for=${id} class="xmc-h-cfg__label">${label}</label>
        <input
          id=${id}
          type="number"
          class="xmc-h-input"
          value=${String(value)}
          onInput=${(e) => {
            const n = Number(e.target.value);
            onChange(Number.isNaN(n) ? value : n);
          }}
        />
        <small class="xmc-h-cfg__hint xmc-h-cfg__hint--path">${path}</small>
      </div>
    `;
  }
  if (Array.isArray(value)) {
    const isStrArr = value.every((x) => typeof x === "string");
    return html`
      <div class="xmc-h-cfg__field">
        <label for=${id} class="xmc-h-cfg__label">${label}
          <span class="xmc-h-cfg__type">[array]</span>
        </label>
        <textarea
          id=${id}
          class="xmc-h-input xmc-h-cfg__textarea"
          rows="3"
          value=${isStrArr ? value.join("\n") : JSON.stringify(value, null, 2)}
          onChange=${(e) => {
            const t = e.target.value;
            if (isStrArr) {
              onChange(t.split("\n").map((s) => s.trim()).filter(Boolean));
            } else {
              try {
                onChange(JSON.parse(t));
              } catch (_) {
                toast.error("数组 JSON 格式不合法：" + label);
              }
            }
          }}
        ></textarea>
        <small class="xmc-h-cfg__hint xmc-h-cfg__hint--path">${path}</small>
      </div>
    `;
  }
  if (value !== null && typeof value === "object") {
    // Nested object — render as collapsible group with recursive AutoFields.
    return html`<${ObjectGroup} value=${value} onChange=${onChange} label=${label} path=${path} />`;
  }
  // string | null
  const str = value == null ? "" : String(value);
  const isSecret = /api_key|secret|token|password/i.test(label);
  return html`
    <div class="xmc-h-cfg__field">
      <label for=${id} class="xmc-h-cfg__label">${label}
        ${isSecret ? html`<span class="xmc-h-badge xmc-h-badge--warning">secret</span>` : null}
      </label>
      <input
        id=${id}
        type=${isSecret ? "password" : "text"}
        class="xmc-h-input"
        value=${str}
        placeholder=${isSecret && /redacted|unset/.test(str) ? "(留空保留现有值)" : ""}
        onInput=${(e) => onChange(e.target.value)}
      />
      <small class="xmc-h-cfg__hint xmc-h-cfg__hint--path">${path}</small>
    </div>
  `;
}

function ObjectGroup({ value, onChange, label, path }) {
  return html`
    <div class="xmc-h-cfg__group">
      <h4 class="xmc-h-cfg__group-title">${label}</h4>
      <div class="xmc-h-cfg__group-body">
        ${Object.entries(value).map(([k, v]) => {
          if (k.startsWith("_")) return null;  // skip _note / _comment fields
          return html`
            <${AutoField}
              key=${k}
              label=${k}
              path=${path + "." + k}
              value=${v}
              onChange=${(nv) => onChange({ ...value, [k]: nv })}
            />
          `;
        })}
      </div>
    </div>
  `;
}

// ── Page ────────────────────────────────────────────────────────

export function ConfigPage({ token }) {
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState(null);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");
  const [activeCat, setActiveCat] = useState(null);
  const [yamlMode, setYamlMode] = useState(false);
  const [yamlText, setYamlText] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    apiGet("/api/v2/config", token)
      .then((d) => {
        const cfg = d.config || {};
        setConfig(cfg);
        setDraft(cfg);
        setYamlText(JSON.stringify(cfg, null, 2));
      })
      .catch((e) => setError(String(e.message || e)));
  }, [token]);

  useEffect(() => { load(); }, [load]);

  const categories = useMemo(() => {
    if (!draft) return [];
    return Object.keys(draft).filter((k) => !k.startsWith("_"));
  }, [draft]);

  useEffect(() => {
    if (!activeCat && categories.length) setActiveCat(categories[0]);
  }, [categories, activeCat]);

  const dirty = useMemo(() => {
    if (!config || !draft) return false;
    return JSON.stringify(config) !== JSON.stringify(draft);
  }, [config, draft]);

  const filteredCategories = useMemo(() => {
    if (!search.trim()) return categories;
    const q = search.toLowerCase();
    return categories.filter((c) => {
      if (c.toLowerCase().includes(q)) return true;
      // Match against the Chinese label too so a user searching "记忆"
      // finds the memory section even though the raw key is English.
      if (catLabel(c).toLowerCase().includes(q)) return true;
      const flat = JSON.stringify(draft?.[c] || {}).toLowerCase();
      return flat.includes(q);
    });
  }, [categories, search, draft]);

  const onCatChange = (cat, newVal) => {
    setDraft((prev) => ({ ...(prev || {}), [cat]: newVal }));
  };

  const onSave = async () => {
    setSaving(true);
    try {
      const body = yamlMode
        ? JSON.parse(yamlText || "{}")
        : draft;
      const res = await fetch(
        "/api/v2/config" + (token ? `?token=${encodeURIComponent(token)}` : ""),
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        }
      );
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
      toast.success("已保存。" + (data.note || ""));
      load();
    } catch (e) {
      toast.error("保存失败：" + (e.message || e));
    } finally {
      setSaving(false);
    }
  };

  const onReset = () => {
    if (!config) return;
    setDraft(config);
    setYamlText(JSON.stringify(config, null, 2));
    toast.info("已重置到当前 daemon 加载值");
  };

  const onExport = () => {
    const blob = new Blob([JSON.stringify(draft, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "xmclaw-config-" + new Date().toISOString().slice(0, 10) + ".json";
    a.click();
    URL.revokeObjectURL(url);
  };

  if (error) {
    return html`
      <section class="xmc-h-page" aria-labelledby="config-title">
        <header class="xmc-h-page__header">
          <h2 id="config-title" class="xmc-h-page__title">配置</h2>
        </header>
        <div class="xmc-h-page__body"><div class="xmc-h-error">${error}</div></div>
      </section>
    `;
  }
  if (draft === null) {
    return html`
      <section class="xmc-h-page" aria-labelledby="config-title">
        <header class="xmc-h-page__header">
          <h2 id="config-title" class="xmc-h-page__title">配置</h2>
        </header>
        <div class="xmc-h-page__body"><div class="xmc-h-loading">载入中…</div></div>
      </section>
    `;
  }

  const activeData = activeCat ? draft[activeCat] : null;

  return html`
    <section class="xmc-h-page" aria-labelledby="config-title">
      <header class="xmc-h-page__header">
        <div class="xmc-h-page__heading">
          <h2 id="config-title" class="xmc-h-page__title">配置</h2>
          <p class="xmc-h-page__subtitle">
            daemon/config.json 的形式编辑器。改完点保存；LLM / runtime
            类改动需要重启 daemon 才会生效。<strong>留空 secret 字段</strong>
            （显示为 redacted）则保留现有值。
          </p>
        </div>
        <div class="xmc-h-page__actions">
          <button
            type="button"
            class="xmc-h-btn"
            onClick=${() => setYamlMode((v) => !v)}
            title="切换 表单 ↔ JSON"
          >
            <${Icon} d=${yamlMode ? I_FORM : I_CODE} />
            ${yamlMode ? "表单" : "JSON"}
          </button>
          <button type="button" class="xmc-h-btn" onClick=${onExport} title="导出为 JSON 文件">
            导出
          </button>
          <button type="button" class="xmc-h-btn" onClick=${onReset} disabled=${!dirty}>
            <${Icon} d=${I_RESET} />
            重置
          </button>
          <button
            type="button"
            class="xmc-h-btn xmc-h-btn--primary"
            onClick=${onSave}
            disabled=${saving || (!dirty && !yamlMode)}
          >
            <${Icon} d=${I_SAVE} />
            ${saving ? "保存中…" : "保存"}
          </button>
        </div>
      </header>

      <div class="xmc-h-page__body xmc-h-cfg__body">
        <aside class="xmc-h-skills__panel" aria-label="categories">
          <div class="xmc-h-skills__panel-head">
            <${Icon} d=${I_FILTER} className="xmc-h-skills__panel-icon" />
            <span>分类</span>
          </div>
          <div class="xmc-h-cfg__searchbar">
            <span class="xmc-h-skills__searchicon"><${Icon} d=${I_SEARCH} /></span>
            <input
              type="search"
              class="xmc-h-input"
              placeholder="搜索字段…"
              value=${search}
              onInput=${(e) => setSearch(e.target.value)}
            />
            ${search ? html`
              <button class="xmc-h-skills__searchclear" onClick=${() => setSearch("")} aria-label="clear">
                <${Icon} d=${I_X} />
              </button>` : null}
          </div>
          <div class="xmc-h-skills__panel-list">
            ${filteredCategories.map((c) => html`
              <button
                key=${c}
                type="button"
                class=${"xmc-h-skills__panelitem " + (activeCat === c ? "is-active" : "")}
                onClick=${() => setActiveCat(c)}
              >
                <span class="xmc-h-cfg__catglyph">${CAT_GLYPHS[c] || "•"}</span>
                <span class="xmc-h-skills__panelitem-label">${catLabel(c)}</span>
              </button>
            `)}
          </div>
        </aside>

        <div class="xmc-h-cfg__content">
          ${yamlMode
            ? html`
              <div class="xmc-h-card">
                <h3 class="xmc-h-card__title">原始 JSON 编辑</h3>
                <textarea
                  class="xmc-h-input xmc-h-cfg__yaml"
                  spellcheck="false"
                  value=${yamlText}
                  onInput=${(e) => setYamlText(e.target.value)}
                ></textarea>
              </div>
            `
            : activeCat == null
              ? html`<div class="xmc-h-empty">选择左侧分类开始编辑</div>`
              : typeof activeData === "object" && activeData !== null && !Array.isArray(activeData)
                ? html`
                  <div class="xmc-h-card">
                    <h3 class="xmc-h-card__title">${catLabel(activeCat)}</h3>
                    <div class="xmc-h-cfg__group-body">
                      ${Object.entries(activeData).map(([k, v]) => {
                        if (k.startsWith("_")) return null;
                        return html`
                          <${AutoField}
                            key=${k}
                            label=${k}
                            path=${activeCat + "." + k}
                            value=${v}
                            onChange=${(nv) => onCatChange(activeCat, { ...activeData, [k]: nv })}
                          />
                        `;
                      })}
                    </div>
                  </div>
                `
                : html`
                  <div class="xmc-h-card">
                    <h3 class="xmc-h-card__title">${catLabel(activeCat)}</h3>
                    <${AutoField}
                      label=${activeCat}
                      path=${activeCat}
                      value=${activeData}
                      onChange=${(nv) => onCatChange(activeCat, nv)}
                    />
                  </div>
                `}
        </div>
      </div>
    </section>
  `;
}
