// XMclaw — Tools page
//
// Lists tools wired into the running AgentLoop (sourced from
// /api/v2/status.tools) plus configured MCP servers. Read-only — adding /
// removing tools still happens via daemon/config.json today.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet } from "../lib/api.js";

export function ToolsPage({ token }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/api/v2/status", token)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [token]);

  if (error) return html`<section class="xmc-datapage"><h2>工具</h2><p class="xmc-datapage__error">${error}</p></section>`;
  if (!data) return html`<section class="xmc-datapage"><p>加载中…</p></section>`;

  const tools = data.tools || [];
  const mcps = data.mcp_servers || [];

  return html`
    <section class="xmc-datapage" aria-labelledby="tools-title">
      <header class="xmc-datapage__header">
        <h2 id="tools-title">工具</h2>
        <p class="xmc-datapage__subtitle">
          已加载 ${tools.length} 个工具，${mcps.length} 个 MCP 服务。
        </p>
      </header>

      <h3 style="margin:1rem 0 .5rem">内置工具</h3>
      ${tools.length === 0
        ? html`<p class="xmc-datapage__empty">未启用工具（config 缺 <code>tools</code> 段）</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${tools.map((name) => html`
                <li class="xmc-datapage__row" key=${name}>
                  <strong>${name}</strong>
                </li>
              `)}
            </ul>
          `}

      <h3 style="margin:1.5rem 0 .5rem">MCP 服务</h3>
      ${mcps.length === 0
        ? html`<p class="xmc-datapage__empty">未配置 MCP 服务</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${mcps.map((name) => html`
                <li class="xmc-datapage__row" key=${name}>
                  <strong>${name}</strong>
                  <small><${Badge} tone="muted">stdio</${Badge}></small>
                </li>
              `)}
            </ul>
          `}
    </section>
  `;
}
