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
        ? html`
            <div class="xmc-datapage__empty" style="padding:1rem;line-height:1.7">
              <p style="margin:0 0 .5rem"><strong>还没有任何 agent 预设。</strong></p>
              <p style="margin:0 0 .5rem;font-size:.85rem">
                XMclaw 默认只跑一个名为 <code>main</code> 的主 agent — 用 <code>config.json</code> 里的 <code>llm</code> 节配置。
              </p>
              <p style="margin:0;font-size:.85rem;color:var(--xmc-fg-muted)">
                这个页面是 Epic #17 的多 agent 注册表。当你需要让多个 agent 协同工作（不同 persona / 不同 LLM / 不同工具集），
                通过 <code>POST /api/v2/agents</code> 创建预设，或者用 <code>chat_with_agent</code> / <code>submit_to_agent</code> 工具让主 agent 派遣子 agent。
                这是高阶玩法，新手可忽略。
              </p>
            </div>
          `
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
