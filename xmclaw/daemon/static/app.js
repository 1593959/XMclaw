// XMclaw — app entry
//
// Phase 0 scaffold only: three-region placeholder shell + routing plumbing
// + bootstrap-source indicator. Phase 1 will replace the placeholder panes
// with real Chat / Sidebar / StatusBar components per FRONTEND_DESIGN.md
// §3.1 and §4.1.
//
// Preact + htm are resolved by bootstrap.js before this module is imported,
// and exposed on window.__xmc. We read them once at top-level so the rest
// of the app just uses `h` / `render` / `html` as locals.

const { h, render } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

import { app as store } from "./store.js";
import { installRouter } from "./router.js";
import { Button } from "./components/atoms/button.js";
import { Badge } from "./components/atoms/badge.js";
import { Icon } from "./components/atoms/icon.js";
import { Avatar } from "./components/atoms/avatar.js";
import { Spinner } from "./components/atoms/spinner.js";

// ── Route stubs ───────────────────────────────────────────────────────
// Each is a no-frills placeholder; Phase 1+ will replace them with the
// real page implementations under ./pages/.

function Placeholder({ title, subtitle }) {
  return html`
    <section class="xmc-placeholder" aria-labelledby="placeholder-title">
      <h2 id="placeholder-title">${title}</h2>
      <p class="xmc-placeholder__subtitle">${subtitle}</p>
      <p class="xmc-placeholder__hint">
        Phase 0 scaffold. See
        <code>docs/FRONTEND_DESIGN.md §4</code> for the full design.
      </p>
    </section>
  `;
}

const routes = {
  "/chat": () => html`<${Placeholder}
    title="Chat"
    subtitle="会话主面板 · 流式 / Plan&Act / @ 上下文 · Phase 1 实现"
  />`,
  "/agents": () => html`<${Placeholder}
    title="Agents"
    subtitle="多 Agent 管理（Epic #17 已落地后端） · Phase 3"
  />`,
  "/skills": () => html`<${Placeholder}
    title="Skills"
    subtitle="Skill 库与版本矩阵 · 进化的可见面 · Phase 3"
  />`,
  "/evolution": () => html`<${Placeholder}
    title="Evolution ★"
    subtitle="差异化锚点 · VFM chart + learned-today feed · Phase 4"
  />`,
  "/memory": () => html`<${Placeholder}
    title="Memory"
    subtitle="三层记忆浏览 + prune · Phase 3"
  />`,
  "/tools": () => html`<${Placeholder}
    title="Tools"
    subtitle="工具清单 + MCP servers · Phase 3"
  />`,
  "/security": () => html`<${Placeholder}
    title="Security"
    subtitle="审批队列 + audit log · Phase 2"
  />`,
  "/backup": () => html`<${Placeholder}
    title="Backup"
    subtitle="备份与恢复 (Epic #20) · Phase 3"
  />`,
  "/doctor": () => html`<${Placeholder}
    title="Doctor"
    subtitle="诊断 + 自修复 (Epic #10) · Phase 3"
  />`,
  "/insights": () => html`<${Placeholder}
    title="Insights"
    subtitle="Usage / Cost / Learning 三 tab · Phase 4"
  />`,
  "/settings": () => html`<${Placeholder}
    title="Settings"
    subtitle="主题 / 快捷键 / i18n · Phase 3"
  />`,
  "*": () => html`<${Placeholder}
    title="Not found"
    subtitle="未匹配的路由"
  />`,
};

// ── Shell ─────────────────────────────────────────────────────────────

const SIDEBAR_ITEMS = [
  { path: "/chat", label: "Chat", icon: "message" },
  { path: "/agents", label: "Agents", icon: "users" },
  { path: "/skills", label: "Skills", icon: "book" },
  { path: "/evolution", label: "Evolution", icon: "sparkle", accent: true },
  { path: "/memory", label: "Memory", icon: "layers" },
  { path: "/tools", label: "Tools", icon: "wrench" },
  { path: "/security", label: "Security", icon: "shield" },
  { path: "/backup", label: "Backup", icon: "archive" },
  { path: "/doctor", label: "Doctor", icon: "stethoscope" },
  { path: "/insights", label: "Insights", icon: "chart" },
  { path: "/settings", label: "Settings", icon: "cog" },
];

function Sidebar({ activePath }) {
  return html`
    <nav class="xmc-sidebar" aria-label="Primary">
      <div class="xmc-sidebar__brand">
        <${Avatar} initials="XM" />
        <strong>XMclaw</strong>
      </div>
      <ul class="xmc-sidebar__list">
        ${SIDEBAR_ITEMS.map(
          (item) => html`
            <li>
              <a
                href=${item.path}
                class=${"xmc-sidebar__item" +
                (item.path === activePath ? " is-active" : "") +
                (item.accent ? " is-accent" : "")}
                aria-current=${item.path === activePath ? "page" : null}
              >
                <${Icon} name=${item.icon} />
                <span>${item.label}</span>
              </a>
            </li>
          `
        )}
      </ul>
    </nav>
  `;
}

function TopBar({ bootstrapSource }) {
  return html`
    <header class="xmc-topbar" role="banner">
      <div class="xmc-topbar__title">XMclaw Web UI</div>
      <div class="xmc-topbar__meta">
        <${Badge} tone="muted">bootstrap: ${bootstrapSource}</${Badge}>
        <${Button} variant="ghost" size="sm">New session</${Button}>
      </div>
    </header>
  `;
}

function StatusBar({ connection }) {
  const tone = connection.status === "connected" ? "success" : "warn";
  return html`
    <footer class="xmc-statusbar" role="contentinfo">
      <${Badge} tone=${tone}>${connection.status}</${Badge}>
      <span class="xmc-statusbar__hint">
        Phase 0 scaffold · WebSocket wiring arrives in Phase 1
      </span>
      <${Spinner} size="sm" label="scaffold" />
    </footer>
  `;
}

function App({ state }) {
  const page = routes[state.route.path] || routes["*"];
  return html`
    <div class="xmc-shell">
      <${TopBar} bootstrapSource=${state.bootstrap.source} />
      <div class="xmc-shell__body">
        <${Sidebar} activePath=${state.route.path} />
        <main class="xmc-main" role="main">${page()}</main>
      </div>
      <${StatusBar} connection=${state.connection} />
    </div>
  `;
}

// ── Mount ─────────────────────────────────────────────────────────────

const root = document.getElementById("root");
root.removeAttribute("aria-busy");

function renderApp() {
  render(html`<${App} state=${store.getState()} />`, root);
}

installRouter(store, routes);
store.subscribe(renderApp);
renderApp();
