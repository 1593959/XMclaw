// XMclaw — 多 Agent 面板（统一入口）  Group G4 (2026-06-06)
//
// 信息架构：一个「多 Agent」总面板，内部分 Tab：
//   · Agent 管理 —— 建/列/编辑/删 agent、看状态（复用 AgentsPage）
//   · 群聊 / 工作流房间 —— 多 agent 协作：工作流编排 / 群聊（复用 RoomsPage）
// 群聊与工作流都装在这个面板内，而非和「代理」并列的独立导航项。

const { h } = window.__xmc.preact;
const { useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { AgentsPage } from "./Agents.js";
import { RoomsPage } from "./Rooms.js";

const TABS = [
  { id: "agents", label: "Agent 管理", hint: "建 / 列 / 删 agent，配人格与模型" },
  { id: "rooms", label: "群聊 · 工作流", hint: "多 agent 协作房间：目标驱动编排 / 群聊" },
];

export function MultiAgentPanel({ token, initialTab }) {
  const [tab, setTab] = useState(initialTab === "rooms" ? "rooms" : "agents");
  return html`
    <div style="display:flex;flex-direction:column;height:100%;min-height:0">
      <nav class="xmc-mem-groupnav" role="tablist" aria-label="多 agent 面板"
           style="padding:14px 20px 0">
        <div class="xmc-mem-group">
          <span class="xmc-mem-group__label">多 AGENT</span>
          <div class="xmc-mem-group__seg">
            ${TABS.map((t) => html`
              <button type="button" role="tab" key=${t.id}
                aria-selected=${t.id === tab}
                class=${"xmc-mem-seg" + (t.id === tab ? " is-active" : "")}
                onClick=${() => setTab(t.id)} title=${t.hint}>${t.label}</button>
            `)}
          </div>
        </div>
      </nav>
      <div style="flex:1;min-height:0;overflow:auto">
        ${tab === "agents" ? html`<${AgentsPage} token=${token} />` : null}
        ${tab === "rooms" ? html`<${RoomsPage} token=${token} />` : null}
      </div>
    </div>
  `;
}
