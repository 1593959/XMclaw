// XMclaw — Agents page
//
// Lists registered agent presets (Epic #17 multi-agent registry) from
// /api/v2/agents. Read-only first cut; create/delete will land later.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet } from "../lib/api.js";

export function AgentsPage({ token }) {
  const [agents, setAgents] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet("/api/v2/agents", token)
      .then((d) => {
        if (cancelled) return;
        const list = Array.isArray(d) ? d : (d && d.agents) || [];
        setAgents(list);
      })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [token]);

  if (error) return html`<section class="xmc-datapage"><h2>智能体</h2><p class="xmc-datapage__error">${error}</p></section>`;
  if (!agents) return html`<section class="xmc-datapage"><p>加载中…</p></section>`;

  return html`
    <section class="xmc-datapage" aria-labelledby="agents-title">
      <header class="xmc-datapage__header">
        <h2 id="agents-title">智能体</h2>
        <p class="xmc-datapage__subtitle">已注册 ${agents.length} 个 agent 预设。</p>
      </header>
      ${agents.length === 0
        ? html`<p class="xmc-datapage__empty">尚无 agent 预设</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${agents.map((a) => html`
                <li class="xmc-datapage__row" key=${a.id || a.agent_id || a.name}>
                  <div style="display:flex;justify-content:space-between;align-items:center">
                    <strong>${a.name || a.id || a.agent_id || "(unnamed)"}</strong>
                    ${a.role ? html`<${Badge} tone="muted">${a.role}</${Badge}>` : null}
                  </div>
                  ${a.system_prompt ? html`<small>${(a.system_prompt + "").slice(0, 120)}…</small>` : null}
                  ${a.model ? html`<small>模型：<code>${a.model}</code></small>` : null}
                </li>
              `)}
            </ul>
          `}
    </section>
  `;
}
