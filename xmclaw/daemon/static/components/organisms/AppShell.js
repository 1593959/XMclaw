// XMclaw — App shell 1:1 port of hermes-agent/web/src/App.tsx
//
// Hermes-spec layout:
//   - Outer flex column, h-dvh, overflow-hidden, bg-black, uppercase,
//     text-midground, antialiased
//   - <Backdrop /> + <PluginSlot name="backdrop" /> as z-1..z-101 layers
//   - Mobile header (lg:hidden) — fixed top, h-12, hamburger + brand
//   - Sidebar — fixed top-left, w-64, h-dvh, bg-background-base/95,
//     backdrop-blur-sm, border-r border-current/20, slides in/out on
//     mobile, sticky on desktop (lg:sticky lg:translate-x-0)
//   - Sidebar contents (top→bottom):
//       - Brand block (h-14, "Hermes / Agent" two-line title)
//       - <PluginSlot name="header-left" />
//       - <nav> with ul of NavLink items
//       - <SidebarSystemActions /> (restart / update gateway)
//       - Footer row: ThemeSwitcher + LanguageSwitcher
//       - <SidebarFooter /> (version + org link)
//   - Main content: relative z-2, flex-col, padded, contains <Routes />

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Backdrop } from "./Backdrop.js";
import { THEMES, applyTheme, readActiveTheme, listThemes } from "../../lib/hermes-themes.js";
import { apiGet } from "../../lib/api.js";
import { confirmDialog } from "../../lib/dialog.js";
import { SetupBanner } from "../molecules/SetupBanner.js";
import { BuddyMascot } from "../molecules/BuddyMascot.js";

// Lucide-style inline SVG icons. Each takes className for sizing.
// Direct shape ports of the Hermes nav-icon set so visual size + stroke
// match. Width / height come from CSS (h-3.5 w-3.5 = 14×14 in the nav).
const ICONS = {
  Terminal:      "M4 17l6-6-6-6M12 19h8",
  MessageSquare: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
  BarChart3:     "M3 3v18h18M7 16V10M12 16V6M17 16v-4",
  FileText:      "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M16 13H8M16 17H8M10 9H8",
  Clock:         "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2",
  Package:       "M16.5 9.4 7.55 4.24M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16zM3.27 6.96 12 12.01l8.73-5.05M12 22.08V12",
  Settings:      "M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2zM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
  KeyRound:      "M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z M16.5 7.5h.01",
  BookOpen:      "M12 7v14M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z",
  Sparkles:      "m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3z M5 3v4 M19 17v4 M3 5h4 M17 19h4",
  Wrench:        "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z",
  Shield:        "M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z",
  Heart:         "M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z",
  RotateCw:      "M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8 M21 3v5h-5",
  Download:      "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3",
  Loader2:       "M21 12a9 9 0 1 1-6.219-8.56",
  Globe:         "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 2a14.5 14.5 0 0 1 0 20 14.5 14.5 0 0 1 0-20M2 12h20",
  Database:      "M3 5a9 3 0 1 0 18 0 9 3 0 1 0-18 0M3 5v14a9 3 0 0 0 18 0V5M3 12a9 3 0 0 0 18 0",
  Zap:           "M13 2 3 14h9l-1 8 10-12h-9l1-8z",
  Star:          "M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z",
  Code:          "m16 18 6-6-6-6 M8 6l-6 6 6 6",
  Eye:           "M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0 M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  Activity:      "M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.5.5 0 0 1-.96 0L9.24 2.18a.5.5 0 0 0-.96 0l-2.35 8.36A2 2 0 0 1 4 12H2",
  Puzzle:        "M15.39 4.39a1 1 0 0 0 1.68-.474 2.5 2.5 0 1 1 3.014 3.015 1 1 0 0 0-.474 1.68l1.683 1.682a2.414 2.414 0 0 1 0 3.414L19.61 15.39a1 1 0 0 1-1.68-.474 2.5 2.5 0 1 0-3.014 3.015 1 1 0 0 1 .474 1.68l-1.683 1.682a2.414 2.414 0 0 1-3.414 0L8.61 19.61a1 1 0 0 0-1.68.474 2.5 2.5 0 1 1-3.014-3.015 1 1 0 0 0 .474-1.68L2.707 13.707a2.414 2.414 0 0 1 0-3.414L4.39 8.61a1 1 0 0 1 1.68.474 2.5 2.5 0 1 0 3.014-3.015 1 1 0 0 1-.474-1.68l1.683-1.682a2.414 2.414 0 0 1 3.414 0z",
  Menu:          "M4 12h16 M4 6h16 M4 18h16",
  X:             "M18 6 6 18 M6 6l12 12",
};

function Icon({ name, className }) {
  const d = ICONS[name] || ICONS.Puzzle;
  return html`
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"
      class=${"xmc-icon " + (className || "")}
      aria-hidden="true"
    >
      <path d=${d} />
    </svg>
  `;
}

// Built-in nav (mirrors Hermes BUILTIN_NAV_REST + CHAT_NAV_ITEM).
// B-151: nav 5 分组 + 折叠
//
// 之前 20 项平铺在侧栏，新手扫一眼眼花。按用途归 5 组:
//   💬 对话/通信  对话 / 会话 / 智能体 / 聊天接入
//   🧠 能力       技能 / 工具 / 记忆 / 进化
//   ⏱ 自动化     Cron / 工作区
//   👁 观察       思考 / 日志 / 分析 / 洞察
//   ⚙ 系统       设置 / 高级配置 / 安全 / 诊断 / 备份 / 文档
//
// 每组可折叠，活跃路由的组自动展开。状态存 localStorage。
const NAV_GROUPS = [
  {
    id: "comm", label: "对话与通信", icon: "Terminal",
    items: [
      { path: "/chat",      label: "对话",     icon: "Terminal" },
      { path: "/sessions",  label: "会话",     icon: "MessageSquare" },
      { path: "/agents",    label: "智能体",   icon: "Heart" },
      { path: "/channels",  label: "聊天接入（入站）", icon: "MessageSquare" },
    ],
  },
  {
    id: "capabilities", label: "能力", icon: "Sparkles",
    items: [
      { path: "/skills",    label: "技能", icon: "Package" },
      { path: "/tools",     label: "工具", icon: "Wrench" },
      { path: "/memory",    label: "记忆", icon: "Database" },
      { path: "/evolution", label: "进化", icon: "Sparkles", accent: true },
    ],
  },
  {
    id: "automation", label: "自动化", icon: "Clock",
    items: [
      { path: "/cron",      label: "Cron",   icon: "Clock" },
      { path: "/workspace", label: "工作区", icon: "FileText" },
    ],
  },
  {
    id: "observe", label: "观察", icon: "Eye",
    items: [
      { path: "/trace",     label: "思考", icon: "Activity" },
      { path: "/logs",      label: "日志", icon: "FileText" },
      { path: "/analytics", label: "分析", icon: "BarChart3" },
      { path: "/insights",  label: "洞察", icon: "Eye" },
    ],
  },
  {
    id: "system", label: "系统", icon: "Settings",
    items: [
      { path: "/settings",  label: "设置",     icon: "Settings" },
      { path: "/security",  label: "安全",     icon: "Shield" },
      { path: "/doctor",    label: "诊断",     icon: "Heart" },
      { path: "/backup",    label: "备份",     icon: "Database" },
      { path: "/config",    label: "高级配置", icon: "Settings" },
      { path: "/docs",      label: "文档",     icon: "BookOpen" },
    ],
  },
];

// Flat list for backward-compat (tests that read NAV_ITEMS for icon
// coverage). Generated from the grouped structure.
const NAV_ITEMS = NAV_GROUPS.flatMap((g) => g.items);

const NAV_COLLAPSE_KEY = "xmc.nav.collapsed_groups";

function readCollapsedGroups() {
  try {
    const raw = localStorage.getItem(NAV_COLLAPSE_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? new Set(arr) : new Set();
  } catch (_) { return new Set(); }
}

function persistCollapsedGroups(set) {
  try {
    localStorage.setItem(NAV_COLLAPSE_KEY, JSON.stringify([...set]));
  } catch (_) { /* private mode — skip */ }
}

function NavLink({ item, active, onClick }) {
  return html`
    <li>
      <a
        href=${item.path}
        class=${"xmc-h-nav__link " + (active ? "is-active" : "")}
        aria-current=${active ? "page" : null}
        onClick=${onClick}
      >
        <${Icon} name=${item.icon} className="xmc-h-nav__icon" />
        <span class="xmc-h-nav__label">${item.label}</span>
        <span aria-hidden="true" class="xmc-h-nav__hover-tint"></span>
        ${active
          ? html`<span aria-hidden="true" class="xmc-h-nav__active-bar blend-lighter"></span>`
          : null}
      </a>
    </li>
  `;
}

// B-151: stateful root that owns per-group collapse, persisted to
// localStorage so the user's chosen layout survives reloads.
function NavRoot({ activePath, onItemClick }) {
  const [collapsed, setCollapsed] = useState(readCollapsedGroups);
  const toggle = (gid) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(gid)) next.delete(gid); else next.add(gid);
      persistCollapsedGroups(next);
      return next;
    });
  };
  return html`
    <ul class="xmc-h-nav__list" style="margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:.15rem">
      ${NAV_GROUPS.map((g) => html`
        <${NavGroup}
          key=${g.id}
          group=${g}
          activePath=${activePath}
          collapsed=${collapsed.has(g.id)}
          onToggle=${() => toggle(g.id)}
          onItemClick=${onItemClick}
        />
      `)}
    </ul>
  `;
}

// B-151: collapsible group section. Always-expanded when an item
// inside is the current route (so the user sees what's active).
function NavGroup({ group, activePath, collapsed, onToggle, onItemClick }) {
  const containsActive = group.items.some((it) => it.path === activePath);
  const isCollapsed = collapsed && !containsActive;
  return html`
    <li class="xmc-h-nav__group">
      <button
        type="button"
        class=${"xmc-h-nav__group-head" + (isCollapsed ? " is-collapsed" : "")}
        onClick=${onToggle}
        aria-expanded=${!isCollapsed}
        title=${isCollapsed ? "展开" : "折叠"}
        style="display:flex;align-items:center;gap:.4rem;width:100%;padding:.4rem .5rem;background:transparent;border:0;color:var(--xmc-fg-muted);font-size:.7rem;letter-spacing:.05em;text-transform:uppercase;cursor:pointer;border-radius:4px"
      >
        <${Icon} name=${group.icon} className="xmc-h-nav__group-icon" />
        <span style="flex:1 1 auto;text-align:left">${group.label}</span>
        <span aria-hidden="true" style="opacity:.6;transform:${isCollapsed ? "rotate(-90deg)" : "rotate(0)"};transition:transform .15s">▾</span>
      </button>
      ${!isCollapsed
        ? html`<ul class="xmc-h-nav__list" style="margin:0;padding:0;list-style:none">
            ${group.items.map((item) => html`
              <${NavLink}
                key=${item.path}
                item=${item}
                active=${activePath === item.path}
                onClick=${onItemClick}
              />
            `)}
          </ul>`
        : null}
    </li>
  `;
}

function ThemeSwitcher({ dropUp }) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(readActiveTheme());
  const items = listThemes();
  const cur = THEMES[active];

  const onPick = (name) => {
    applyTheme(name);
    setActive(name);
    setOpen(false);
  };

  return html`
    <div class=${"xmc-h-themesw " + (open ? "is-open" : "") + (dropUp ? " xmc-h-themesw--up" : "")}>
      <button
        type="button"
        class="xmc-h-themesw__btn"
        aria-haspopup="listbox"
        aria-expanded=${open ? "true" : "false"}
        onClick=${() => setOpen((v) => !v)}
        title=${"主题: " + (cur?.label || active)}
      >
        <${Icon} name="Sparkles" />
        <span class="xmc-h-themesw__label">${cur?.label || active}</span>
      </button>
      ${open
        ? html`
          <ul class="xmc-h-themesw__menu" role="listbox">
            ${items.map(
              (t) => html`
                <li
                  key=${t.name}
                  role="option"
                  aria-selected=${t.name === active}
                  class=${"xmc-h-themesw__opt " + (t.name === active ? "is-active" : "")}
                  onClick=${() => onPick(t.name)}
                >
                  <strong>${t.label}</strong>
                  <small>${t.description}</small>
                </li>
              `
            )}
          </ul>
        `
        : null}
    </div>
  `;
}

function LanguageSwitcher() {
  // Hermes has a real i18n. Stub for now — visual placement preserved.
  return html`
    <button type="button" class="xmc-h-langsw" title="language">
      <${Icon} name="Globe" />
      <span>ZH</span>
    </button>
  `;
}

function SidebarFooter() {
  return html`
    <div class="xmc-h-sidefooter">
      <span class="xmc-h-sidefooter__ver font-mono-ui">v0.2.0</span>
      <a
        href="https://github.com/1593959/XMclaw"
        target="_blank"
        rel="noopener noreferrer"
        class="xmc-h-sidefooter__org blend-lighter"
      >XMclaw</a>
    </div>
  `;
}

function SidebarSystemActions({ token }) {
  // Wired to /api/v2/system/{restart,upgrade}. Restart fires a detached
  // relauncher and exits the current daemon; the UI shows a "正在重启…"
  // overlay until /status responds again. Upgrade kicks off pip in the
  // background; the same hook polls /upgrade/status so the user sees
  // "升级中…" → "升级完成（请点重启）".
  const [busyKind, setBusyKind] = useState(null); // "restart" | "upgrade" | null
  const [upgradeState, setUpgradeState] = useState(null);
  // Restart progress: when set, we keep polling /api/v2/status until
  // it answers, then clear.
  const [restartTick, setRestartTick] = useState(0);

  const onRestart = async () => {
    const ok = await confirmDialog({
      title: "重启 daemon",
      body: "当前会话连接会断开，约 3 秒后恢复。",
      confirmLabel: "重启",
    });
    if (!ok) return;
    setBusyKind("restart");
    try {
      const url = "/api/v2/system/restart" +
        (token ? `?token=${encodeURIComponent(token)}` : "");
      await fetch(url, { method: "POST" }).catch(() => null);
      // Daemon is going down. Poll /status until it's back.
      let attempts = 0;
      const poll = async () => {
        attempts += 1;
        try {
          const r = await fetch(
            "/api/v2/status" +
            (token ? `?token=${encodeURIComponent(token)}` : ""),
            { cache: "no-store" },
          );
          if (r.ok) {
            setBusyKind(null);
            setRestartTick(0);
            // Force a reload so WS reconnects against the new daemon.
            window.location.reload();
            return;
          }
        } catch (_) { /* still down — fine */ }
        setRestartTick(attempts);
        if (attempts < 60) setTimeout(poll, 800);
        else setBusyKind(null);
      };
      setTimeout(poll, 1500);
    } catch (_) {
      setBusyKind(null);
    }
  };

  const onUpgrade = async () => {
    const ok = await confirmDialog({
      title: "升级 XMclaw",
      body: "执行 pip install --upgrade xmclaw。\n升级完成后需要点 '重启 daemon' 才会加载新版本。",
      confirmLabel: "升级",
    });
    if (!ok) return;
    setBusyKind("upgrade");
    setUpgradeState({ phase: "starting", tail: [] });
    try {
      const url = "/api/v2/system/upgrade" +
        (token ? `?token=${encodeURIComponent(token)}` : "");
      const res = await fetch(url, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setUpgradeState({ phase: "error", error: data.error || `HTTP ${res.status}` });
        setBusyKind(null);
        return;
      }
      // Poll status every 1.5s until process exits.
      const poll = async () => {
        try {
          const r = await fetch(
            "/api/v2/system/upgrade/status" +
            (token ? `?token=${encodeURIComponent(token)}` : ""),
          );
          const s = await r.json();
          setUpgradeState({
            phase: s.running ? "running" : "done",
            tail: s.log_tail || [],
            returncode: s.returncode,
          });
          if (s.running) {
            setTimeout(poll, 1500);
          } else {
            setBusyKind(null);
          }
        } catch (_) {
          setTimeout(poll, 2000);
        }
      };
      setTimeout(poll, 1500);
    } catch (e) {
      setUpgradeState({ phase: "error", error: String(e.message || e) });
      setBusyKind(null);
    }
  };

  const dismissUpgrade = () => setUpgradeState(null);

  return html`
    <div class="xmc-h-sysact">
      <span class="xmc-h-sysact__label">系统</span>
      <ul class="xmc-h-sysact__list">
        <li>
          <button
            type="button"
            class="xmc-h-sysact__btn"
            onClick=${onRestart}
            disabled=${busyKind != null}
            title="POST /api/v2/system/restart"
          >
            <${Icon} name="RotateCw" className="xmc-h-nav__icon" />
            <span>${busyKind === "restart" ? `重启中 (${restartTick})` : "重启 daemon"}</span>
          </button>
        </li>
        <li>
          <button
            type="button"
            class="xmc-h-sysact__btn"
            onClick=${onUpgrade}
            disabled=${busyKind != null}
            title="POST /api/v2/system/upgrade"
          >
            <${Icon} name="Download" className="xmc-h-nav__icon" />
            <span>${busyKind === "upgrade" ? "升级中…" : "更新 XMclaw"}</span>
          </button>
        </li>
      </ul>
      ${upgradeState ? html`
        <div class="xmc-h-sysact__panel" role="status" aria-live="polite">
          <div class="xmc-h-sysact__panel-head">
            <strong>升级状态：${
              upgradeState.phase === "starting" ? "启动中"
              : upgradeState.phase === "running" ? "运行中"
              : upgradeState.phase === "done" ? (upgradeState.returncode === 0 ? "成功（请点 重启 daemon）" : `失败 rc=${upgradeState.returncode}`)
              : upgradeState.phase === "error" ? "出错" : upgradeState.phase
            }</strong>
            <button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${dismissUpgrade}>关闭</button>
          </div>
          ${upgradeState.error ? html`<p class="xmc-datapage__error">${upgradeState.error}</p>` : null}
          ${upgradeState.tail && upgradeState.tail.length ? html`
            <pre class="xmc-h-sysact__log">${upgradeState.tail.slice(-12).join("\n")}</pre>
          ` : null}
        </div>
      ` : null}
    </div>
  `;
}

function _useDaemonStatus(token) {
  // Lightweight daemon-status hook used by the top of the sidebar to
  // surface the agent's active workspace + model. Polls every 30s so
  // a workspace switch via /workspace shows up in chrome without a
  // page reload. Best-effort — failures keep the last-known value.
  const [status, setStatus] = useState(null);
  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      apiGet("/api/v2/status", token)
        .then((d) => { if (!cancelled) setStatus(d); })
        .catch(() => { /* keep prior */ });
    };
    tick();
    const id = setInterval(tick, 30_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [token]);
  return status;
}

function _basename(path) {
  if (!path) return "";
  const parts = String(path).split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

// B-107: humanise large token counts. 12_345 → "12.3k", 1_234_567 → "1.23M".
function _fmtTokens(n) {
  const v = Number(n) || 0;
  if (v < 1000) return String(v);
  if (v < 1_000_000) return (v / 1000).toFixed(1) + "k";
  return (v / 1_000_000).toFixed(2) + "M";
}

function _fmtUsd(n) {
  const v = Number(n) || 0;
  if (v === 0) return "—";
  if (v < 0.01) return "<¢1";
  if (v < 1) return `$${v.toFixed(3)}`;
  return `$${v.toFixed(2)}`;
}

function ContextStrip({ status, tokenUsage }) {
  if (!status) return null;
  const wsActive = status.workspace?.active;
  const wsName = wsActive ? _basename(wsActive) : null;
  // B-107: only render token rows when we've seen at least one
  // COST_TICK this session (turns > 0). Empty session = quiet UI.
  const hasUsage = tokenUsage && tokenUsage.turns > 0;
  const totalTokens = hasUsage
    ? (tokenUsage.prompt_tokens + tokenUsage.completion_tokens) : 0;
  // Show a budget bar only when the daemon set a non-zero budget.
  const showBudget = hasUsage && tokenUsage.budget_usd > 0;
  const budgetPct = showBudget
    ? Math.min(100, (tokenUsage.spent_usd / tokenUsage.budget_usd) * 100)
    : 0;
  const budgetTone = budgetPct > 90 ? "warn" : budgetPct > 70 ? "info" : "muted";
  return html`
    <div class="xmc-h-sidebar__contextstrip" title=${wsActive || ""}>
      <div class="xmc-h-sidebar__ctx-row" title=${wsActive || "(无)"}>
        <span class="xmc-h-sidebar__ctx-key">workspace</span>
        <span class="xmc-h-sidebar__ctx-val">${wsName || "—"}</span>
      </div>
      <div class="xmc-h-sidebar__ctx-row" title=${status.model || ""}>
        <span class="xmc-h-sidebar__ctx-key">model</span>
        <span class="xmc-h-sidebar__ctx-val">${status.model || "—"}</span>
      </div>
      <div class="xmc-h-sidebar__ctx-row">
        <span class="xmc-h-sidebar__ctx-key">tools</span>
        <span class="xmc-h-sidebar__ctx-val">${(status.tools || []).length}</span>
      </div>
      ${hasUsage ? html`
        <div class="xmc-h-sidebar__ctx-row" title=${`prompt ${tokenUsage.prompt_tokens} + completion ${tokenUsage.completion_tokens}`}>
          <span class="xmc-h-sidebar__ctx-key">tokens</span>
          <span class="xmc-h-sidebar__ctx-val">${_fmtTokens(totalTokens)}</span>
        </div>
        <div class="xmc-h-sidebar__ctx-row" title=${`已花费 ${tokenUsage.spent_usd.toFixed(4)} 美元${showBudget ? ` / 预算 ${tokenUsage.budget_usd.toFixed(2)} 美元` : ""}`}>
          <span class="xmc-h-sidebar__ctx-key">cost</span>
          <span class="xmc-h-sidebar__ctx-val">${_fmtUsd(tokenUsage.spent_usd)}</span>
        </div>
        ${showBudget ? html`
          <div class="xmc-h-sidebar__ctx-budget" aria-label=${`预算消耗 ${budgetPct.toFixed(1)}%`}>
            <div
              class=${"xmc-h-sidebar__ctx-budget-fill is-" + budgetTone}
              style=${"width:" + budgetPct.toFixed(1) + "%"}
            ></div>
          </div>
        ` : null}
      ` : null}
    </div>
  `;
}

export function AppShell({ activePath, brand = "XMclaw", subBrand = "Agent", token, tokenUsage, children }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const closeMobile = useCallback(() => setMobileOpen(false), []);
  const status = _useDaemonStatus(token);

  // Close on Escape (Hermes parity).
  useEffect(() => {
    if (!mobileOpen) return;
    const onKey = (e) => { if (e.key === "Escape") setMobileOpen(false); };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [mobileOpen]);

  // Auto-close on viewport ≥ 1024 (lg breakpoint, Hermes parity).
  useEffect(() => {
    const mql = window.matchMedia("(min-width: 1024px)");
    const onChange = (e) => { if (e.matches) setMobileOpen(false); };
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  return html`
    <div class="xmc-h-app">
      <${Backdrop} />

      <header class="xmc-h-mobheader">
        <button
          type="button"
          class="xmc-h-mobheader__btn"
          onClick=${() => setMobileOpen(true)}
          aria-label="open navigation"
        >
          <${Icon} name="Menu" />
        </button>
        <span class="xmc-h-mobheader__brand blend-lighter">${brand}</span>
      </header>

      ${mobileOpen
        ? html`<button type="button" class="xmc-h-overlay" onClick=${closeMobile} aria-label="close navigation"></button>`
        : null}

      <div class="xmc-h-shell-body">
        <aside
          id="app-sidebar"
          class=${"xmc-h-sidebar " + (mobileOpen ? "is-mobile-open" : "")}
          aria-label="primary navigation"
        >
          <div class="xmc-h-sidebar__brand">
            <strong class="xmc-h-sidebar__title blend-lighter">
              ${brand}<br/>${subBrand}
            </strong>
            <button
              type="button"
              class="xmc-h-sidebar__close"
              onClick=${closeMobile}
              aria-label="close navigation"
            >
              <${Icon} name="X" />
            </button>
          </div>

          <${ContextStrip} status=${status} tokenUsage=${tokenUsage} />

          <nav class="xmc-h-nav" aria-label="primary navigation">
            <${NavRoot}
              activePath=${activePath === "/" ? "/sessions" : activePath}
              onItemClick=${closeMobile}
            />
          </nav>

          <${SidebarSystemActions} token=${token} />

          <div class="xmc-h-sidebar__footrow">
            <${ThemeSwitcher} dropUp=${true} />
            <${LanguageSwitcher} />
          </div>

          <${SidebarFooter} />
        </aside>

        <main class="xmc-h-main" role="main">
          <${SetupBanner} token=${token} />
          ${children}
        </main>
      </div>
      <${BuddyMascot} />
    </div>
  `;
}
