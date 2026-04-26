// XMclaw — Doctor page
//
// Read-only view of /api/v2/status + /health. Mirrors what `xmclaw doctor`
// reports from the CLI but in the running-daemon's own voice (no offline
// checks). For a deeper config-side audit, run `xmclaw doctor --fix` from
// the terminal.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet } from "../lib/api.js";

export function DoctorPage({ token }) {
  const [data, setData] = useState(null);
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [s, h] = await Promise.all([
          apiGet("/api/v2/status", token),
          apiGet("/health", token),
        ]);
        if (cancelled) return;
        setData(s);
        setHealth(h);
        setError(null);
      } catch (exc) {
        if (!cancelled) setError(String(exc.message || exc));
      }
    }
    load();
    const id = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [token]);

  if (error) {
    return html`
      <section class="xmc-datapage">
        <h2>诊断</h2>
        <p class="xmc-datapage__error">${error}</p>
      </section>
    `;
  }
  if (!data || !health) {
    return html`<section class="xmc-datapage"><p>加载中…</p></section>`;
  }

  const checks = [
    { label: "Daemon 健康", ok: health.status === "ok", detail: `version ${health.version}` },
    { label: "Agent 已绑定 LLM", ok: !!data.agent_wired, detail: data.model || "未绑定" },
    { label: "鉴权", ok: !!data.auth_required, detail: data.auth_required ? "pairing token" : "no-auth" },
    { label: "工具", ok: (data.tools || []).length > 0, detail: `${(data.tools || []).length} 个` },
    { label: "MCP 服务", ok: (data.mcp_servers || []).length > 0, detail: (data.mcp_servers || []).join(", ") || "未配置" },
  ];

  return html`
    <section class="xmc-datapage" aria-labelledby="doctor-title">
      <header class="xmc-datapage__header">
        <h2 id="doctor-title">诊断</h2>
        <p class="xmc-datapage__subtitle">运行中的 daemon 自检（每 5 秒刷新）。</p>
      </header>
      <ul class="xmc-datapage__list">
        ${checks.map((c) => html`
          <li class="xmc-datapage__row" key=${c.label}>
            <div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem">
              <strong>${c.label}</strong>
              <${Badge} tone=${c.ok ? "success" : "warn"}>${c.ok ? "OK" : "异常"}</${Badge}>
            </div>
            <small>${c.detail}</small>
          </li>
        `)}
      </ul>
    </section>
  `;
}
