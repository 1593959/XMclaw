// XMclaw — first-time setup banner (B-81)
//
// Polls /api/v2/setup and surfaces missing onboarding steps as a
// dismissable banner at the top of the main content area. The endpoint
// returns ``{llm_configured, persona_ready, embedding_configured,
// indexer_running, dream_running, missing: [...], ready: bool}`` —
// we surface up to three actionable items with a quick-jump button
// each.
//
// Dismiss state: localStorage "xmc-setup-dismissed-set" stores a
// JSON-encoded set of missing-item names the user already chose to
// ignore. A new missing item NOT in that set re-shows the banner —
// so dismissing "embedding" once doesn't hide future "llm" reminders.
//
// "ready: true" hides the banner unconditionally and clears the
// dismiss set so a future regression (e.g. user wipes config) gets
// surfaced fresh.

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../../lib/api.js";
import { toast } from "../../lib/toast.js";

const DISMISS_KEY = "xmc-setup-dismissed-set";

// Per-missing-item descriptor: label + (Chinese) what's broken + a
// quick-jump callback. Ordered by priority — LLM > persona > embedding.
// ``form`` field opts the row into B-83's inline-expand UX: rather
// than navigating away, clicking the action toggles a form right in
// the banner. ``href`` is the fallback for items without an inline
// form (persona requires a CLI command, embedding lives on its own
// dedicated page that already has an inline form).
const STEP_INFO = {
  llm: {
    title: "未配置 LLM API key",
    body: "Agent 当前以 echo 模式运行（只回显消息），需要至少一个 provider 的 API key 才能真正对话。",
    action: "立即配置",
    form: "llm",  // B-83: inline form
  },
  persona: {
    title: "Persona 文件未初始化",
    body: "首次安装需要运行 xmclaw onboard 创建 SOUL.md / IDENTITY.md。Agent 没有这些文件就缺少身份和工作目标。",
    action: "复制命令",
    href: null,  // copy-to-clipboard handler
    copyCmd: "xmclaw onboard",
  },
  embedding: {
    title: "向量索引未启用",
    body: "memory_search 当前只能做关键词匹配。配置一个 embedding provider 后会获得真正的语义检索。",
    action: "立即配置",
    form: "embedding",  // B-84: inline form (twin of B-83 LLM form)
  },
};

function _readDismissed() {
  try {
    const raw = localStorage.getItem(DISMISS_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr : []);
  } catch (_) {
    return new Set();
  }
}

function _writeDismissed(set) {
  try {
    localStorage.setItem(DISMISS_KEY, JSON.stringify(Array.from(set)));
  } catch (_) {
    /* quota / private mode — silently no-op */
  }
}

export function SetupBanner({ token }) {
  const [setup, setSetup] = useState(null);
  const [dismissed, setDismissed] = useState(_readDismissed);
  // B-83: which inline form (if any) is currently expanded.
  const [openForm, setOpenForm] = useState(null);
  // Form state for the LLM panel.
  const [llmForm, setLlmForm] = useState({
    provider: "anthropic",
    api_key: "",
    base_url: "",
    default_model: "",
  });
  const [llmSaving, setLlmSaving] = useState(false);
  // B-84: form state for the embedding panel. Defaults are pre-filled
  // for the local Ollama path (qwen3-embedding:0.6b @ 1024) — same
  // defaults as the Memory page's inline form (B-76).
  const [embForm, setEmbForm] = useState({
    provider: "openai",
    base_url: "http://127.0.0.1:11434/v1",
    model: "qwen3-embedding:0.6b",
    dimensions: 1024,
    api_key: "",
  });
  const [embSaving, setEmbSaving] = useState(false);

  const reload = useCallback(() => {
    apiGet("/api/v2/setup", token)
      .then(setSetup)
      .catch(() => setSetup(null));
  }, [token]);

  useEffect(() => {
    reload();
    const id = setInterval(reload, 60_000);
    return () => clearInterval(id);
  }, [reload]);

  // When the daemon reports ready=true, clear any sticky dismiss state
  // so a future regression (e.g. user wipes config.json) re-surfaces
  // the banner without requiring them to remember they dismissed it.
  useEffect(() => {
    if (setup && setup.ready && dismissed.size > 0) {
      _writeDismissed(new Set());
      setDismissed(new Set());
    }
  }, [setup, dismissed]);

  // B-86: detect "config saved on disk but daemon hasn't reloaded".
  // The setup endpoint reports embedding_configured (cfg dict has the
  // section) AND indexer_running (lifespan actually constructed an
  // embedder + vec provider). If the first is true but the second is
  // false, the most common reason is the user edited config.json or
  // saved via the inline form but forgot to restart the daemon —
  // surface that explicitly so the next move is obvious.
  const restartPending =
    setup &&
    setup.embedding_configured === true &&
    setup.indexer_running === false;

  // Banner shows when EITHER there's something missing OR a restart is
  // pending. Once everything is missing-clean AND running, hide.
  if (!setup) return null;
  if (setup.ready && !restartPending) return null;

  // Filter out items the user has explicitly dismissed.
  const visible = (setup.missing || []).filter((m) => !dismissed.has(m));
  if (visible.length === 0 && !restartPending) return null;

  const onDismiss = (key) => {
    const next = new Set(dismissed);
    next.add(key);
    setDismissed(next);
    _writeDismissed(next);
  };

  const onDismissAll = () => {
    const next = new Set([...(setup.missing || [])]);
    setDismissed(next);
    _writeDismissed(next);
  };

  const onCopy = async (cmd) => {
    try {
      await navigator.clipboard.writeText(cmd);
      toast.success(`已复制到剪贴板：${cmd}`);
    } catch (_) {
      toast.info(`手动复制：${cmd}`);
    }
  };

  // B-84: submit the inline embedding form. Hits the same endpoint
  // as Memory → Providers → 配置 embedding (B-76).
  const onSaveEmbedding = async () => {
    if (!embForm.model.trim()) {
      toast.error("model 不能为空");
      return;
    }
    if (!embForm.dimensions || embForm.dimensions <= 0) {
      toast.error("dimensions 必须 > 0（要和模型实际输出维度一致）");
      return;
    }
    setEmbSaving(true);
    try {
      const url = "/api/v2/memory/embedding/configure" +
        (token ? `?token=${encodeURIComponent(token)}` : "");
      const res = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(embForm),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      toast.success("已保存 — 重启 daemon 生效");
      setOpenForm(null);
      reload();
    } catch (e) {
      toast.error("保存失败：" + (e.message || e));
    } finally {
      setEmbSaving(false);
    }
  };

  // B-83: submit the inline LLM form. Mirrors the embedding-configure
  // flow on the Memory page (B-76).
  const onSaveLLM = async () => {
    if (!llmForm.api_key.trim()) {
      toast.error("API key 不能为空");
      return;
    }
    setLlmSaving(true);
    try {
      const url = "/api/v2/llm/configure" +
        (token ? `?token=${encodeURIComponent(token)}` : "");
      const res = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(llmForm),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      toast.success("已保存 — 重启 daemon 生效");
      setOpenForm(null);
      // Re-fetch setup so the banner reflects the new state on next tick.
      reload();
    } catch (e) {
      toast.error("保存失败：" + (e.message || e));
    } finally {
      setLlmSaving(false);
    }
  };

  return html`
    <div
      class="xmc-h-setupbanner"
      role="status"
      aria-label="首次设置进度"
      style="margin:0 0 .9rem 0;padding:.7rem .9rem;border:1px solid var(--color-warning, #c8a86a);border-radius:6px;background:rgba(200,168,106,.08);font-family:var(--xmc-font-mono);font-size:.82rem"
    >
      <div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem;margin-bottom:.4rem">
        <strong style="letter-spacing:.05em;text-transform:uppercase;color:var(--color-warning, #c8a86a)">
          ${visible.length > 0
            ? html`首次设置进度（${visible.length} / ${(setup.missing || []).length} 待完成）`
            : html`配置已保存，等待 daemon 重启`}
        </strong>
        ${visible.length > 0 ? html`
          <button
            type="button"
            class="xmc-h-btn xmc-h-btn--ghost"
            style="font-size:.7rem;padding:.1rem .5rem"
            onClick=${onDismissAll}
            title="全部忽略，恢复进度后会自动重新提示"
          >
            全部忽略
          </button>
        ` : null}
      </div>
      ${visible.map((key) => {
        const info = STEP_INFO[key];
        if (!info) return null;
        const expanded = openForm === key;
        // Action button:
        //   - info.form  → toggles inline form
        //   - info.href  → plain navigation link
        //   - info.copyCmd → copy-to-clipboard
        let actionBtn;
        if (info.form === "llm" || info.form === "embedding") {
          actionBtn = html`
            <button
              type="button"
              class="xmc-h-btn xmc-h-btn--primary"
              style="font-size:.72rem;padding:.2rem .55rem"
              onClick=${() => setOpenForm(expanded ? null : info.form)}
            >
              ${expanded ? "收起" : info.action}
            </button>
          `;
        } else if (info.href) {
          actionBtn = html`
            <a
              href=${info.href}
              class="xmc-h-btn xmc-h-btn--primary"
              style="font-size:.72rem;padding:.2rem .55rem;text-decoration:none"
            >
              ${info.action} →
            </a>
          `;
        } else {
          actionBtn = html`
            <button
              type="button"
              class="xmc-h-btn xmc-h-btn--primary"
              style="font-size:.72rem;padding:.2rem .55rem"
              onClick=${() => onCopy(info.copyCmd)}
            >
              ${info.action}
            </button>
          `;
        }
        return html`
          <div
            key=${key}
            style="padding:.4rem 0;border-top:1px dashed rgba(200,168,106,.25)"
          >
            <div style="display:flex;justify-content:space-between;align-items:center;gap:.6rem">
              <div style="flex:1;min-width:0">
                <div style="font-weight:600">⚠ ${info.title}</div>
                <div style="margin-top:.2rem;color:var(--xmc-fg-muted);font-size:.76rem;line-height:1.5">
                  ${info.body}
                </div>
              </div>
              <div style="display:flex;gap:.3rem;flex-shrink:0">
                ${actionBtn}
                <button
                  type="button"
                  class="xmc-h-btn xmc-h-btn--ghost"
                  style="font-size:.72rem;padding:.2rem .5rem"
                  onClick=${() => onDismiss(key)}
                  title="只忽略这一项"
                >
                  忽略
                </button>
              </div>
            </div>
            ${expanded && info.form === "llm" ? html`
              <div style="margin-top:.6rem;display:grid;grid-template-columns:auto 1fr;gap:.4rem .6rem;align-items:center;font-size:.78rem;padding:.5rem;background:rgba(0,0,0,.2);border-radius:4px">
                <label>provider</label>
                <select
                  value=${llmForm.provider}
                  onChange=${(e) => setLlmForm({ ...llmForm, provider: e.target.value })}
                  class="xmc-h-input"
                >
                  <option value="anthropic">anthropic (Claude)</option>
                  <option value="openai">openai (GPT 兼容协议)</option>
                </select>
                <label>api_key</label>
                <input
                  type="password"
                  class="xmc-h-input"
                  value=${llmForm.api_key}
                  placeholder=${llmForm.provider === "anthropic" ? "sk-ant-..." : "sk-..."}
                  onInput=${(e) => setLlmForm({ ...llmForm, api_key: e.target.value })}
                  autocomplete="new-password"
                />
                <label>base_url</label>
                <input
                  type="text"
                  class="xmc-h-input"
                  value=${llmForm.base_url}
                  placeholder=${llmForm.provider === "anthropic" ? "https://api.anthropic.com（默认）" : "https://api.openai.com/v1（默认）"}
                  onInput=${(e) => setLlmForm({ ...llmForm, base_url: e.target.value })}
                />
                <label>default_model</label>
                <input
                  type="text"
                  class="xmc-h-input"
                  value=${llmForm.default_model}
                  placeholder=${llmForm.provider === "anthropic" ? "claude-sonnet-4 / claude-opus-4 / ..." : "gpt-4.1 / gpt-4.1-mini / ..."}
                  onInput=${(e) => setLlmForm({ ...llmForm, default_model: e.target.value })}
                />
              </div>
              <div style="margin-top:.5rem;display:flex;gap:.4rem;justify-content:flex-end">
                <button
                  type="button"
                  class="xmc-h-btn xmc-h-btn--ghost"
                  style="font-size:.75rem"
                  onClick=${() => setOpenForm(null)}
                >取消</button>
                <button
                  type="button"
                  class="xmc-h-btn xmc-h-btn--primary"
                  style="font-size:.75rem"
                  disabled=${llmSaving}
                  onClick=${onSaveLLM}
                >${llmSaving ? "保存中…" : "保存（需重启 daemon）"}</button>
              </div>
              <div style="margin-top:.4rem;font-size:.7rem;color:var(--xmc-fg-muted)">
                提示：base_url 和 default_model 是可选的；不填用 provider 默认值。<br/>
                想用第三方兼容服务（MiniMax / DashScope / Moonshot 等）？把它们的 base_url 填上，把对应模型 ID 填到 default_model。
              </div>
            ` : null}
            ${expanded && info.form === "embedding" ? html`
              <div style="margin-top:.6rem;display:grid;grid-template-columns:auto 1fr;gap:.4rem .6rem;align-items:center;font-size:.78rem;padding:.5rem;background:rgba(0,0,0,.2);border-radius:4px">
                <label>provider</label>
                <select
                  value=${embForm.provider}
                  onChange=${(e) => setEmbForm({ ...embForm, provider: e.target.value })}
                  class="xmc-h-input"
                >
                  <option value="openai">openai (兼容 OpenAI / Ollama / vLLM / DashScope)</option>
                </select>
                <label>base_url</label>
                <input
                  type="text"
                  class="xmc-h-input"
                  value=${embForm.base_url}
                  placeholder="http://127.0.0.1:11434/v1"
                  onInput=${(e) => setEmbForm({ ...embForm, base_url: e.target.value })}
                />
                <label>model</label>
                <input
                  type="text"
                  class="xmc-h-input"
                  value=${embForm.model}
                  placeholder="qwen3-embedding:0.6b"
                  onInput=${(e) => setEmbForm({ ...embForm, model: e.target.value })}
                />
                <label>dimensions</label>
                <input
                  type="number"
                  class="xmc-h-input"
                  value=${embForm.dimensions}
                  min="1"
                  onInput=${(e) => setEmbForm({ ...embForm, dimensions: Number(e.target.value) || 0 })}
                />
                <label>api_key</label>
                <input
                  type="password"
                  class="xmc-h-input"
                  value=${embForm.api_key}
                  placeholder="（Ollama 本地不需要）"
                  onInput=${(e) => setEmbForm({ ...embForm, api_key: e.target.value })}
                  autocomplete="new-password"
                />
              </div>
              <div style="margin-top:.5rem;display:flex;gap:.4rem;justify-content:flex-end">
                <button
                  type="button"
                  class="xmc-h-btn xmc-h-btn--ghost"
                  style="font-size:.75rem"
                  onClick=${() => setOpenForm(null)}
                >取消</button>
                <button
                  type="button"
                  class="xmc-h-btn xmc-h-btn--primary"
                  style="font-size:.75rem"
                  disabled=${embSaving}
                  onClick=${onSaveEmbedding}
                >${embSaving ? "保存中…" : "保存（需重启 daemon）"}</button>
              </div>
              <div style="margin-top:.4rem;font-size:.7rem;color:var(--xmc-fg-muted)">
                提示：dimensions 必须和模型实际输出维度一致 — qwen3-embedding:0.6b = 1024，
                text-embedding-3-small = 1536，bge-m3 = 1024。错位会让向量表悄悄写脏。
              </div>
            ` : null}
          </div>
        `;
      })}
      ${restartPending ? html`
        <div
          style="margin-top:.5rem;padding:.5rem .7rem;border-top:1px dashed rgba(200,168,106,.25);display:flex;justify-content:space-between;align-items:center;gap:.6rem;flex-wrap:wrap"
        >
          <div style="flex:1;min-width:0">
            ${setup.indexer_start_error ? html`
              <!-- B-87: indexer try/catch surfaced a concrete reason — show it. -->
              <div style="font-weight:600;color:#e77f7f">⚠ 向量索引启动失败（daemon 已重启过，但 indexer 起不来）</div>
              <div style="margin-top:.3rem;font-size:.76rem;line-height:1.5;color:var(--xmc-fg-muted)">
                <strong style="color:var(--xmc-fg)">原因：</strong>
                <code style="font-size:.74rem;white-space:pre-wrap">${setup.indexer_start_error}</code>
              </div>
              <div style="margin-top:.3rem;font-size:.74rem;line-height:1.55;color:var(--xmc-fg-muted)">
                常见对应修法：
                <ul style="margin:.2rem 0 0 1.1rem;padding:0">
                  <li>Ollama 没起来 → 在终端跑 <code>ollama serve</code>（或检查 base_url）</li>
                  <li>模型本地没拉 → <code>ollama pull qwen3-embedding:0.6b</code></li>
                  <li>维度跟历史数据冲突 → 删 <code>~/.xmclaw/v2/memory.db</code> 重启（会丢已有向量索引，不会丢 MEMORY.md）</li>
                </ul>
              </div>
            ` : html`
              <div style="font-weight:600">🔄 daemon 未重启 — 已保存的配置还没生效</div>
              <div style="margin-top:.2rem;color:var(--xmc-fg-muted);font-size:.76rem;line-height:1.5">
                你已经把 embedding 配置写到了磁盘，但 daemon 是冷加载的——
                它内存里那一份 config 是 <code>xmclaw start</code> 那一刻的快照。
                在终端跑 <code>xmclaw stop &amp;&amp; xmclaw start</code>
                之后向量索引会真正启动，本提示自动消失。
              </div>
            `}
          </div>
          <div style="display:flex;gap:.3rem;flex-shrink:0">
            ${setup.indexer_start_error ? html`
              <a
                href="/ui/doctor"
                class="xmc-h-btn xmc-h-btn--primary"
                style="font-size:.72rem;padding:.2rem .55rem;text-decoration:none"
              >
                打开 Doctor →
              </a>
            ` : html`
              <button
                type="button"
                class="xmc-h-btn xmc-h-btn--primary"
                style="font-size:.72rem;padding:.2rem .55rem"
                onClick=${() => onCopy("xmclaw stop && xmclaw start")}
              >
                复制重启命令
              </button>
            `}
          </div>
        </div>
      ` : null}
    </div>
  `;
}
