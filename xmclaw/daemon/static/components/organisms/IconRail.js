// XMclaw — IconRail (48px left icon navigation bar)
//
// Quick-access icons for the most-used pages. Shows on desktop only;
// mobile users get the full sidebar overlay.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

import { Icon } from "./AppShell.js";

const RAIL_ITEMS = [
  { path: "/dashboard", label: "概览", icon: "BarChart3" },
  { path: "/chat",      label: "对话", icon: "Terminal" },
  { path: "/skills",    label: "技能", icon: "Package" },
  { path: "/agents",    label: "代理", icon: "Users" },
  { path: "/settings",  label: "设置", icon: "Settings" },
];

export function IconRail({ activePath, sidebarCollapsed, onToggleSidebar, onNavigate }) {
  return html`
    <nav class="xmc-h-rail" aria-label="icon navigation">
      <div class="xmc-h-rail__brand" title="XMclaw">X</div>
      <div class="xmc-h-rail__sep"></div>
      ${RAIL_ITEMS.map((item) => html`
        <a
          key=${item.path}
          href=${item.path}
          class=${"xmc-h-rail__btn " + (activePath === item.path ? "is-active" : "")}
          title=${item.label}
          onClick=${(e) => { e.preventDefault(); onNavigate(item.path); }}
        >
          <${Icon} name=${item.icon} />
        </a>
      `)}
      <div class="xmc-h-rail__spacer"></div>
      <button
        type="button"
        class="xmc-h-rail__toggle"
        title=${sidebarCollapsed ? "展开侧边栏" : "折叠侧边栏"}
        onClick=${onToggleSidebar}
      >
        <${Icon} name=${sidebarCollapsed ? "ChevronRight" : "ChevronLeft"} />
      </button>
    </nav>
  `;
}
