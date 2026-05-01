// XMclaw — Channels page (B-147)
//
// Configure 飞书 / 钉钉 / 企微 / Telegram bots from the UI without
// hand-editing daemon/config.json. Each manifest renders a card with:
//   - status badge (ready / scaffold)
//   - running indicator (green dot when adapter is live)
//   - per-field input (typed by config_schema entry suffix:
//     "(required)" / "secret (..)" / "(optional)")
//   - Save button → PUT /api/v2/channels/<id>
//
// Empty secret fields preserve the on-disk value (UI shows "已设置"
// badge and an empty input — same convention as 设置 / 高级配置).

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";

async function putJson(path, token, body) {
  const url = path + (token ? `?token=${encodeURIComponent(token)}` : "");
  const r = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error || d.ok === false) {
    throw new Error(d.error || `HTTP ${r.status}`);
  }
  return d;
}

// schema entry: "string (required)" / "secret (required)" /
// "string (optional, ...)" — extract the type + required flag.
function parseSchemaEntry(entry) {
  const s = String(entry || "");
  const isSecret = /^secret\b/i.test(s);
  const isRequired = /\(required\)/i.test(s);
  return { isSecret, isRequired, hint: s };
}

function ChannelCard({ ch, token, onSaved }) {
  const initialValues = () => {
    const v = {};
    for (const key of Object.keys(ch.config_schema || {})) {
      const cur = (ch.config || {})[key];
      v[key] = (typeof cur === "string" || typeof cur === "number" || typeof cur === "boolean")
        ? String(cur)
        : "";
    }
    if (typeof (ch.config || {}).enabled === "boolean") {
      v.__enabled = ch.config.enabled;
    } else {
      v.__enabled = false;
    }
    return v;
  };

  const [values, setValues] = useState(initialValues);
  const [busy, setBusy] = useState(false);
  // Re-init when the upstream `ch` changes (e.g. after a successful save reload)
  useEffect(() => { setValues(initialValues()); }, [JSON.stringify(ch)]);

  const isReady = ch.implementation_status === "ready";
  const isRunning = !!ch.running;

  const onSave = async () => {
    setBusy(true);
    try {
      const body = { enabled: !!values.__enabled };
      for (const key of Object.keys(ch.config_schema || {})) {
        const v = values[key];
        // Don't send the redacted form back
        if (typeof v === "string" && v.includes("…")) continue;
        body[key] = v ?? "";
      }
      await putJson(`/api/v2/channels/${encodeURIComponent(ch.id)}`, token, body);
      toast.success(`${ch.label} 已保存 — 重启 daemon 后 adapter 重新绑凭据`);
      onSaved();
    } catch (err) {
      toast.error("保存失败：" + (err.message || err));
    } finally {
      setBusy(false);
    }
  };

  const fields = Object.entries(ch.config_schema || {});

  return html`
    <article style="margin:.6rem 0;padding:.7rem;border:1px solid var(--color-border);border-radius:6px;background:color-mix(in srgb, var(--midground) 4%, transparent)">
      <header style="display:flex;justify-content:space-between;align-items:baseline;gap:.5rem;flex-wrap:wrap">
        <strong style="font-size:1rem">${ch.label}</strong>
        <span style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap">
          ${isRunning ? html`<${Badge} tone="success" title="adapter 运行中">● 运行中</${Badge}>` : null}
          <${Badge} tone=${isReady ? "info" : "warn"} title=${isReady ? "已实现" : "仅 manifest，adapter 还没写"}>
            ${isReady ? "ready" : "scaffold"}
          </${Badge}>
          ${ch.needs_tunnel ? html`<${Badge} tone="muted" title="需要公网 IP / cloudflared 隧道">需 tunnel</${Badge}>` : null}
          <code style="font-size:.7rem;color:var(--xmc-fg-muted)">${ch.id}</code>
        </span>
      </header>
      ${(ch.requires || []).length
        ? html`<small style="display:block;margin-top:.2rem;color:var(--xmc-fg-muted);font-size:.7rem">
            依赖: ${ch.requires.map((r) => html`<code>${r}</code> `)}
          </small>`
        : null}
      ${!isReady
        ? html`<div style="margin-top:.5rem;padding:.5rem .65rem;border:1px solid color-mix(in srgb, var(--color-destructive, #c66) 50%, transparent);border-radius:4px;background:color-mix(in srgb, var(--color-destructive, #c66) 8%, transparent);font-size:.78rem">
            <strong>⚠ 未实现</strong> — manifest 已注册但 adapter Python 模块还没写。
            填配置无意义：启动 daemon 不会拉起 adapter，群里 @bot 也不会回话。
            等 ${ch.id} 升级为 ready 状态后再来配。
          </div>`
        : null}

      <div style="display:flex;align-items:center;gap:.5rem;margin-top:.6rem;padding:.4rem .55rem;border-radius:4px;background:color-mix(in srgb, var(--color-primary) 6%, transparent)">
        <label style="display:flex;align-items:center;gap:.4rem;cursor:pointer;font-size:.85rem">
          <input type="checkbox"
                 checked=${!!values.__enabled}
                 onChange=${(e) => setValues({ ...values, __enabled: e.target.checked })} />
          启用此 channel (重启 daemon 生效)
        </label>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(280px, 1fr));gap:.5rem;margin-top:.5rem">
        ${fields.map(([key, schemaEntry]) => {
          const meta = parseSchemaEntry(schemaEntry);
          const cur = (ch.config || {})[key];
          const placeholder = meta.isSecret && cur && typeof cur === "string" && cur.includes("…")
            ? "已设置 (留空保留)"
            : meta.hint;
          return html`
            <label style="display:flex;flex-direction:column;gap:.15rem;font-size:.78rem" key=${key}>
              <span style="font-weight:500">
                ${key}${meta.isRequired ? html`<span style="color:var(--color-error,#c66)"> *</span>` : null}
                ${meta.isSecret && cur && typeof cur === "string" && cur.includes("…")
                  ? html`<${Badge} tone="success" style="margin-left:.3rem">已设置</${Badge}>`
                  : null}
              </span>
              <input
                type=${meta.isSecret ? "password" : "text"}
                value=${meta.isSecret ? "" : (values[key] || "")}
                placeholder=${placeholder}
                onInput=${(e) => setValues({ ...values, [key]: e.target.value })}
                style="padding:.3rem .45rem;font-family:var(--xmc-font-mono);font-size:.75rem;border:1px solid var(--color-border);border-radius:4px;background:var(--color-card);color:var(--color-fg)"
              />
              <small style="font-size:.65rem;color:var(--xmc-fg-muted)">${meta.hint}</small>
            </label>
          `;
        })}
      </div>

      <div style="margin-top:.6rem;display:flex;gap:.4rem;align-items:center">
        <button
          class="xmc-h-btn"
          onClick=${onSave}
          disabled=${busy || !isReady}
          title=${isReady ? "保存配置 (重启 daemon 生效)" : "scaffold 状态无法保存 — adapter 还没实现"}
        >${busy ? "保存中…" : (isReady ? "保存" : "保存 (已禁用)")}</button>
        ${!isReady
          ? html`<small style="opacity:.7;font-size:.7rem">${ch.id} adapter 升级为 ready 前，保存按钮禁用以免误以为生效。</small>`
          : null}
      </div>
    </article>
  `;
}

export function ChannelsPage({ token }) {
  const [channels, setChannels] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(() => {
    apiGet("/api/v2/channels", token)
      .then((d) => { setChannels(d.channels || []); setError(null); })
      .catch((e) => setError(String(e.message || e)));
  }, [token]);

  useEffect(load, [load]);

  if (error) return html`<section class="xmc-datapage"><h2>外部聊天接入</h2><p class="xmc-datapage__error">${error}</p></section>`;
  if (!channels) return html`<section class="xmc-datapage"><p>加载中…</p></section>`;

  const readyCount = channels.filter((c) => c.implementation_status === "ready").length;
  const runningCount = channels.filter((c) => c.running).length;

  return html`
    <section class="xmc-datapage" aria-labelledby="channels-title">
      <header class="xmc-datapage__header">
        <h2 id="channels-title">聊天接入（入站）</h2>
        <p class="xmc-datapage__subtitle">
          <strong>入站方向</strong>：第三方群里 @机器人 → bot 走 agent
          流程答话回去。区别于<strong>出站集成</strong>（agent 主动调
          <code>feishu_send</code>/<code>slack_send</code> 发通知 — 那个在
          <a href="#/config">高级配置 → integrations</a> 配凭据）。
          已实现 ${readyCount} / ${channels.length}，运行中 ${runningCount}。
          <strong>改完保存后需重启 daemon</strong>（adapter 启动时绑凭据）。
        </p>
      </header>
      ${channels.map((ch) => html`<${ChannelCard} ch=${ch} token=${token} onSaved=${load} />`)}
    </section>
  `;
}
