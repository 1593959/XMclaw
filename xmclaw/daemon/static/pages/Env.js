// XMclaw — EnvPage layout port of hermes-agent EnvPage.tsx
//
// Hermes EnvPage manages secrets in ~/.hermes/env.json (one card per
// provider). XMclaw stores secrets inline in daemon/config.json under
// llm.<provider>.api_key. We use the existing PUT /api/v2/config/llm
// endpoint which already handles per-provider writes.
//
// Layout: a Card per known provider, each with rows for api_key /
// base_url / default_model + Save button. Visual structure matches
// Hermes (Card title with provider icon, masked password input,
// hint text below).

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet, apiPost } from "../lib/api.js";
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

const I_KEY  = "M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z M16.5 7.5h.01";
const I_SAVE = "M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2zM17 21v-8H7v8 M7 3v5h8";

const PROVIDERS = [
  {
    id: "anthropic",
    label: "Anthropic（含 MiniMax / DeepSeek 等兼容端点）",
    sample_models: "claude-opus-4-7 · claude-sonnet-4-6 · claude-haiku-4-5-20251001",
    sample_base_url: "https://api.anthropic.com   (留空使用默认)",
  },
  {
    id: "openai",
    label: "OpenAI（含 GLM / Kimi / Ollama / vLLM 等兼容端点）",
    sample_models: "gpt-4o · gpt-4o-mini · o1-mini",
    sample_base_url: "https://api.openai.com/v1   (留空使用默认)",
  },
];

function ProviderCard({ provider, current, token, onSaved }) {
  const cur = current || {};
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(cur.base_url || "");
  const [defaultModel, setDefaultModel] = useState(cur.default_model || "");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    setBaseUrl(cur.base_url || "");
    setDefaultModel(cur.default_model || "");
  }, [cur.base_url, cur.default_model]);

  const keyState = (() => {
    const v = cur.api_key;
    if (typeof v !== "string") return "unset";
    if (v.startsWith("<redacted")) return "set";
    if (v === "<unset>") return "unset";
    if (v.length > 0) return "set";
    return "unset";
  })();

  const onSave = async () => {
    setSaving(true);
    try {
      const body = {
        provider: provider.id,
        default_model: defaultModel.trim(),
        base_url: baseUrl.trim(),
        api_key: apiKey,  // empty string means "keep existing" per backend
      };
      const res = await fetch(
        "/api/v2/config/llm" + (token ? `?token=${encodeURIComponent(token)}` : ""),
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        }
      );
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);
      toast.success(`${provider.id} 保存成功 ✓`);
      setApiKey("");
      onSaved && onSaved();
    } catch (e) {
      toast.error("保存失败：" + (e.message || e));
    } finally {
      setSaving(false);
    }
  };

  return html`
    <div class="xmc-h-card xmc-h-env__provider">
      <h3 class="xmc-h-card__title xmc-h-env__provider-title">
        <${Icon} d=${I_KEY} />
        ${provider.label}
      </h3>

      <div class="xmc-h-cfg__group-body">
        <div class="xmc-h-cfg__field">
          <label class="xmc-h-cfg__label">
            API key
            <span class=${"xmc-h-badge xmc-h-badge--" + (keyState === "set" ? "success" : "warning")}>
              ${keyState === "set" ? "已设置" : "未设置"}
            </span>
          </label>
          <input
            type="password"
            class="xmc-h-input"
            value=${apiKey}
            onInput=${(e) => setApiKey(e.target.value)}
            placeholder=${keyState === "set" ? "(留空保留现有 key) ····" : "粘贴新 API key"}
          />
          <small class="xmc-h-cfg__hint">
            落到 <code>daemon/config.json → llm.${provider.id}.api_key</code>。
          </small>
        </div>

        <div class="xmc-h-cfg__field">
          <label class="xmc-h-cfg__label">Base URL</label>
          <input
            type="text"
            class="xmc-h-input"
            value=${baseUrl}
            onInput=${(e) => setBaseUrl(e.target.value)}
            placeholder=${provider.sample_base_url}
          />
        </div>

        <div class="xmc-h-cfg__field">
          <label class="xmc-h-cfg__label">默认模型 *</label>
          <input
            type="text"
            class="xmc-h-input"
            value=${defaultModel}
            onInput=${(e) => setDefaultModel(e.target.value)}
            placeholder=${provider.sample_models}
          />
        </div>

        <div class="xmc-h-env__actions">
          <button
            type="button"
            class="xmc-h-btn xmc-h-btn--primary"
            onClick=${onSave}
            disabled=${saving || !defaultModel.trim()}
          >
            <${Icon} d=${I_SAVE} />
            ${saving ? "保存中…" : "保存 " + provider.id}
          </button>
        </div>
      </div>
    </div>
  `;
}

// B-104: secrets store panel (file/encrypted backend, env override
// detection). Lives at the bottom of the Env page since it's the
// secondary mechanism — most users will use the LLM provider cards
// above this.
function SecretsPanel({ token }) {
  const [items, setItems] = useState(null);
  const [encryption, setEncryption] = useState(false);
  const [error, setError] = useState(null);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    apiGet("/api/v2/secrets", token)
      .then((d) => {
        setItems(Array.isArray(d.items) ? d.items : []);
        setEncryption(!!d.encryption_available);
      })
      .catch((e) => setError(String(e.message || e)));
  }, [token]);

  useEffect(() => { load(); }, [load]);

  const onSet = async () => {
    if (!name.trim() || !value) return;
    setBusy(true);
    try {
      const r = await apiPost("/api/v2/secrets", { name: name.trim(), value }, token);
      if (r.ok) {
        toast.success(`已保存 — backend: ${r.backend}`);
        setName("");
        setValue("");
        load();
      } else {
        toast.error("保存失败：" + (r.error || "未知"));
      }
    } catch (e) {
      toast.error("保存失败：" + (e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (n) => {
    try {
      const url = `/api/v2/secrets/${encodeURIComponent(n)}` + (token ? `?token=${encodeURIComponent(token)}` : "");
      const res = await fetch(url, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      toast.success(`已删除 ${n}`);
      load();
    } catch (e) {
      toast.error("删除失败：" + (e.message || e));
    }
  };

  return html`
    <div class="xmc-h-card">
      <h3 class="xmc-h-card__title">Secrets 存储（B-104）</h3>
      <p class="xmc-h-cfg__hint">
        独立于 config.json 的密钥仓库。优先级：env 变量 <code>XMC_SECRET_&lt;NAME&gt;</code> &gt;
        secrets 文件 &gt; keyring。${encryption ? "加密后端可用。" : "仅明文文件后端（cryptography 未安装）。"}
        Agent 启动时按 ``llm.&lt;provider&gt;.api_key`` → ``llm.anthropic.api_key`` 这种 dotted name 查询。
      </p>
      ${error ? html`<div class="xmc-h-error">${error}</div>` : null}
      <div style="display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.6rem">
        <input
          type="text"
          placeholder="名称（如 llm.anthropic.api_key）"
          value=${name}
          onInput=${(e) => setName(e.target.value)}
          style="flex:1 1 240px;min-width:0;padding:.4rem .6rem;font-family:var(--xmc-font-mono);font-size:.82rem"
        />
        <input
          type="password"
          placeholder="值"
          value=${value}
          onInput=${(e) => setValue(e.target.value)}
          autocomplete="new-password"
          style="flex:1 1 200px;min-width:0;padding:.4rem .6rem;font-family:var(--xmc-font-mono);font-size:.82rem"
        />
        <button type="button" class="xmc-h-btn xmc-h-btn--primary" onClick=${onSet} disabled=${busy || !name.trim() || !value}>
          ${busy ? "保存中…" : "保存"}
        </button>
      </div>
      ${items === null
        ? html`<div class="xmc-h-loading">载入中…</div>`
        : items.length === 0
          ? html`<p class="xmc-h-cfg__hint">尚无密钥。在上面输入 name + value → 保存。</p>`
          : html`
              <ul style="margin:0;padding:0;list-style:none">
                ${items.map((it) => html`
                  <li
                    key=${it.name}
                    style="display:flex;justify-content:space-between;align-items:center;gap:.5rem;padding:.4rem .5rem;border-top:1px dashed var(--color-border)"
                  >
                    <code style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis">${it.name}</code>
                    ${it.env_only
                      ? html`<small style="color:var(--xmc-warn,#c8a86a)">只在环境变量里（XMC_SECRET_${it.name.replace(/\./g,'_').toUpperCase()}）</small>`
                      : it.env_override
                        ? html`<small style="color:var(--xmc-warn,#c8a86a)">env 变量覆盖中</small>`
                        : null}
                    ${!it.env_only ? html`
                      <button type="button" class="xmc-h-btn xmc-h-btn--ghost" style="font-size:.7rem;padding:.15rem .5rem"
                        onClick=${() => onDelete(it.name)}>删除</button>
                    ` : null}
                  </li>
                `)}
              </ul>
            `}
    </div>
  `;
}

export function EnvPage({ token }) {
  const [config, setConfig] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    apiGet("/api/v2/config", token)
      .then((d) => setConfig(d.config || {}))
      .catch((e) => setError(String(e.message || e)));
  }, [token]);

  useEffect(() => { load(); }, [load]);

  if (error) {
    return html`
      <section class="xmc-h-page" aria-labelledby="env-title">
        <header class="xmc-h-page__header">
          <h2 id="env-title" class="xmc-h-page__title">密钥</h2>
        </header>
        <div class="xmc-h-page__body"><div class="xmc-h-error">${error}</div></div>
      </section>
    `;
  }
  if (config === null) {
    return html`
      <section class="xmc-h-page" aria-labelledby="env-title">
        <header class="xmc-h-page__header">
          <h2 id="env-title" class="xmc-h-page__title">密钥</h2>
        </header>
        <div class="xmc-h-page__body"><div class="xmc-h-loading">载入中…</div></div>
      </section>
    `;
  }

  const llm = (config && config.llm) || {};

  return html`
    <section class="xmc-h-page" aria-labelledby="env-title">
      <header class="xmc-h-page__header">
        <div class="xmc-h-page__heading">
          <h2 id="env-title" class="xmc-h-page__title">密钥</h2>
          <p class="xmc-h-page__subtitle">
            LLM 提供商凭据。<strong>API key 字段留空</strong>则保留现有值；要清空必须显式输入空格再清掉。
            其他字段（base_url / default_model）会立即覆盖到 daemon/config.json。
            <strong>LLM 改动需重启 daemon 才生效。</strong>
          </p>
        </div>
      </header>

      <div class="xmc-h-page__body xmc-h-env__body">
        ${PROVIDERS.map((p) => html`
          <${ProviderCard}
            key=${p.id}
            provider=${p}
            current=${llm[p.id]}
            token=${token}
            onSaved=${load}
          />
        `)}

        <div class="xmc-h-card">
          <h3 class="xmc-h-card__title">关于多模型 profiles</h3>
          <p class="xmc-h-cfg__hint">
            高级用户可以在 <code>llm.profiles[]</code> 配 N 个 profile，每个独立
            api_key / base_url / model；前端 Chat 顶栏可以会话粒度切换。
            管理 profile 走 <code>POST/DELETE /api/v2/llm/profiles</code> —
            "配置" 页 (左栏 llm 类目) 直接可编辑底层 JSON。
          </p>
        </div>

        <${SecretsPanel} token=${token} />
      </div>
    </section>
  `;
}
