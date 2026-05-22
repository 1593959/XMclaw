// XMclaw — ConfigViewer page (Phase F)
//
// Read-only config.json viewer with syntax-highlighted JSON display.
// Config is already sanitized server-side (api_key / bot_token / password
// redacted by /api/v2/config), so this page is safe to expose.
//
// Features:
//   - Pretty-printed JSON with collapsible top-level sections
//   - Copy-to-clipboard button
//   - Config file path display
//   - Read-only badge (explicitly not an editor — edits go through
//     Settings page or manual daemon/config.json edit + restart)

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";
import { t } from "../lib/i18n.js";

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);
  const onClick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      toast.success(t("common.copied"));
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("复制失败");
    }
  };
  return html`
    <button
      type="button"
      class="xmc-btn xmc-btn--sm xmc-btn--ghost"
      onClick=${onClick}
      disabled=${copied}
    >
      ${copied ? "✓ " + t("common.copied") : t("common.copy")}
    </button>
  `;
}

function JsonBlock({ obj }) {
  const text = JSON.stringify(obj, null, 2);
  return html`
    <div style="position:relative">
      <div style="position:absolute;top:.5rem;right:.5rem">
        <${CopyButton} text=${text} />
      </div>
      <pre
        class="xmc-codeblock"
        style="margin:0;padding:1rem;background:var(--xmc-bg-elevated);border-radius:6px;overflow-x:auto;font-size:.78rem;line-height:1.5;max-height:70vh"
      ><code>${text}</code></pre>
    </div>
  `;
}

export function ConfigViewerPage({ token }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!token) return;
    apiGet("/api/v2/config", token)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [token]);

  if (loading) {
    return html`
      <section class="xmc-datapage" aria-labelledby="config-title">
        <header class="xmc-datapage__header">
          <h2 id="config-title">${t("nav.config")}</h2>
          <p class="xmc-datapage__subtitle">当前生效的 daemon 配置（只读）</p>
        </header>
        <p>${t("common.loading")}</p>
      </section>
    `;
  }

  if (error) {
    return html`
      <section class="xmc-datapage" aria-labelledby="config-title">
        <header class="xmc-datapage__header">
          <h2 id="config-title">${t("nav.config")}</h2>
        </header>
        <div class="xmc-dash__err">${t("common.error")}：${error}</div>
      </section>
    `;
  }

  const cfg = data && data.config;
  const cfgPath = data && data.config_path;

  return html`
    <section class="xmc-datapage" aria-labelledby="config-title">
      <header class="xmc-datapage__header">
        <h2 id="config-title">${t("nav.config")}</h2>
        <p class="xmc-datapage__subtitle">
          当前生效的 daemon 配置（只读）· 敏感字段已被服务端脱敏
        </p>
      </header>

      <div style="display:flex;gap:.5rem;align-items:center;margin-bottom:1rem;flex-wrap:wrap">
        <span class="xmc-h-badge xmc-h-badge--muted">只读</span>
        ${cfgPath
          ? html`<code style="font-size:.78rem;opacity:.7">${cfgPath}</code>`
          : null}
        <span style="flex:1"></span>
        <small style="opacity:.6">
          如需修改配置，请编辑上述文件并重启 daemon，或通过设置页修改子项
        </small>
      </div>

      ${cfg == null
        ? html`<div class="xmc-dash__empty">daemon 未加载配置文件</div>`
        : html`<${JsonBlock} obj=${cfg} />`}
    </section>
  `;
}
