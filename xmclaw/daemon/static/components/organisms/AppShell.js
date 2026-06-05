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
const { useState, useEffect, useCallback, useRef } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Backdrop } from "./Backdrop.js";
import { SetupBanner } from "../molecules/SetupBanner.js";
import { BuddyMascot } from "../molecules/BuddyMascot.js";
// B-323: NavRoot / ThemeSwitcher / LanguageSwitcher / SidebarFooter /
// SidebarSystemActions / ContextStrip / useDaemonStatus split into
// AppShellParts.js to keep this file under the 500-line UI budget
// (FRONTEND_DESIGN.md §1.4). ICONS map + Icon + NAV_GROUPS / NAV_ITEMS
// / collapse helpers stay HERE (and are re-exported below) because
// the v2 ui_scaffold test reads icon coverage from this file. ESM
// circular import is fine — every cross-edge is consumed lazily
// inside a function body.
import {
  NavRoot,
  ThemeSwitcher,
  LanguageSwitcher,
  SidebarFooter,
  SidebarSystemActions,
  ContextStrip,
  useDaemonStatus,
} from "./AppShellParts.js";
import { IconRail } from "./IconRail.js";
import { AppHeader } from "./AppHeader.js";
import { ClawHUD } from "../molecules/ClawHUD.js";
import { CommPanel } from "../molecules/CommPanel.js";
import { NotificationPanel } from "../molecules/NotificationPanel.js";

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
  Users:         "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M16 3.128a4 4 0 0 1 0 7.744M22 21v-2a4 4 0 0 0-3-3.87M9 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
  Link:          "M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71",
  ChevronRight:  "M9 18l6-6-6-6",
  ChevronLeft:   "M15 18l-6-6 6-6",
  ClawMark:      "M6 21 C7 13 7 7 5 3 M12 22 C13 13 13 6 11 2 M18 21 C19 13 19 7 17 3",
};

export function Icon({ name, className }) {
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
// Phase F: nav 4 分组 + 折叠 — 从 21 项合并到 14 项，减少认知负担。
//
//   💬 核心       对话 / 会话
//   🧠 智能       技能 / 技能商店 / 进化 / 认知 / 记忆
//   ⚙️ 配置       设置 / 文件 / 定时任务 / 工作区
//   👁️ 观察       分析 / 事件 / 日志
//
// Phase F (2026-05-22): /agents /channels /tools /security /docs 恢复
// 导航入口 — 页面本身有独立功能，不应埋没在设置页内。
// /doctor /backup /config 仍合并到设置页（功能重叠）。
// 每组可折叠，活跃路由的组自动展开。状态存 localStorage。
export const NAV_GROUPS = [
  {
    id: "comm", label: "核心", icon: "Terminal",
    items: [
      { path: "/dashboard", label: "概览", icon: "BarChart3" },
      { path: "/chat",      label: "对话", icon: "Terminal" },
      { path: "/sessions",  label: "会话", icon: "MessageSquare" },
      { path: "/channels",  label: "通道", icon: "Link" },
    ],
  },
  {
    id: "capabilities", label: "智能", icon: "Sparkles",
    items: [
      { path: "/skills",      label: "技能",     icon: "Package" },
      { path: "/marketplace", label: "技能商店", icon: "Package" },
      { path: "/evolution",   label: "进化",     icon: "Sparkles", accent: true },
      { path: "/cognition",   label: "认知",     icon: "Eye" },
      { path: "/memory",      label: "记忆",     icon: "Database" },
      { path: "/agents",      label: "代理",     icon: "Users" },
      { path: "/tools",       label: "工具",     icon: "Wrench" },
    ],
  },
  {
    id: "system", label: "配置", icon: "Settings",
    items: [
      { path: "/settings",  label: "设置",     icon: "Settings" },
      { path: "/security",  label: "安全",     icon: "Shield" },
      { path: "/files",     label: "文件",     icon: "FileText" },
      { path: "/cron",      label: "定时任务", icon: "Clock" },
      { path: "/workspace", label: "工作区",   icon: "FileText" },
    ],
  },
  {
    id: "observe", label: "观察", icon: "Eye",
    items: [
      { path: "/analytics", label: "分析", icon: "BarChart3" },
      { path: "/trace",     label: "事件", icon: "Activity" },
      { path: "/logs",      label: "日志", icon: "FileText" },
      { path: "/docs",      label: "文档", icon: "BookOpen" },
    ],
  },
];

// Flat list for backward-compat (tests that read NAV_ITEMS for icon
// coverage). Generated from the grouped structure.
const NAV_ITEMS = NAV_GROUPS.flatMap((g) => g.items);

const NAV_COLLAPSE_KEY = "xmc.nav.collapsed_groups";

export function readCollapsedGroups() {
  try {
    const raw = localStorage.getItem(NAV_COLLAPSE_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? new Set(arr) : new Set();
  } catch (_) { return new Set(); }
}

export function persistCollapsedGroups(set) {
  try {
    localStorage.setItem(NAV_COLLAPSE_KEY, JSON.stringify([...set]));
  } catch (_) { /* private mode — skip */ }
}


const SIDEBAR_COLLAPSE_KEY = "xmc.sidebar.collapsed";

function readSidebarCollapsed() {
  try { return localStorage.getItem(SIDEBAR_COLLAPSE_KEY) === "1"; }
  catch (_) { return false; }
}

function persistSidebarCollapsed(v) {
  try { localStorage.setItem(SIDEBAR_COLLAPSE_KEY, v ? "1" : "0"); }
  catch (_) {}
}

export function AppShell({ activePath, brand = "XMclaw", subBrand, token, tokenUsage, onNewSession, children }) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(readSidebarCollapsed);
  const [commOpen, setCommOpen] = useState(false);
  const [notifOpen, setNotifOpen] = useState(false);
  const [focusMode, setFocusMode] = useState(false);
  const closeMobile = useCallback(() => setMobileOpen(false), []);
  const status = useDaemonStatus(token);

  // Detect current theme for sub-brand label
  const currentTheme = typeof document !== "undefined"
    ? document.documentElement.getAttribute("data-theme") || "dark"
    : "dark";
  const resolvedSubBrand = subBrand || (currentTheme === "nebula" ? "NEBULA EDITION" : "Agent");

  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed((prev) => {
      const next = !prev;
      persistSidebarCollapsed(next);
      return next;
    });
  }, []);

  const navigateTo = useCallback((path) => {
    window.dispatchEvent(new CustomEvent("xmc-navigate", { detail: { path } }));
  }, []);

  // Worker F (2026-06-05): Header panel toggles + focus mode.
  const toggleComm = useCallback(() => setCommOpen((v) => !v), []);
  const toggleNotif = useCallback(() => setNotifOpen((v) => !v), []);
  const toggleFocus = useCallback(() => setFocusMode((v) => !v), []);

  // Close panels on Escape.
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") {
        setCommOpen(false);
        setNotifOpen(false);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // Close panels on click outside (debounced so the open-click doesn't close).
  useEffect(() => {
    if (!commOpen && !notifOpen) return;
    const onClick = (e) => {
      const target = e.target;
      if (target.closest(".nb-comm-panel") || target.closest(".nb-notif-panel")) return;
      if (target.closest(".nb-header-btn")) return;
      setCommOpen(false);
      setNotifOpen(false);
    };
    const id = setTimeout(() => document.addEventListener("click", onClick), 50);
    return () => {
      clearTimeout(id);
      document.removeEventListener("click", onClick);
    };
  }, [commOpen, notifOpen]);

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

  // 2026-05-12 (FRONTEND_REWORK §3 iter 5 line 151): left-swipe gesture
  // to close the mobile sidebar. Tap-on-overlay already worked; this
  // adds the iOS / Android-native "drag the sheet to dismiss" affordance.
  //
  // Heuristic: dx < -60px AND |dy| < |dx| (more horizontal than vertical)
  // AND total elapsed < 700ms (gesture, not slow drag). Thresholds match
  // common mobile UX defaults (Material has ~48px; we go a touch larger
  // since the sidebar is narrow and accidental swipes during scroll are
  // worse than missed intentional ones).
  const touchRef = useRef({ x: 0, y: 0, t: 0 });
  const onSidebarTouchStart = useCallback((e) => {
    if (!mobileOpen) return;
    const t = e.touches && e.touches[0];
    if (!t) return;
    touchRef.current = { x: t.clientX, y: t.clientY, t: Date.now() };
  }, [mobileOpen]);
  const onSidebarTouchEnd = useCallback((e) => {
    if (!mobileOpen) return;
    const t = (e.changedTouches && e.changedTouches[0]) || null;
    if (!t) return;
    const start = touchRef.current;
    if (!start || !start.t) return;
    const dx = t.clientX - start.x;
    const dy = t.clientY - start.y;
    const dt = Date.now() - start.t;
    if (dt > 700) return;            // too slow → not a gesture
    if (Math.abs(dy) >= Math.abs(dx)) return; // vertical-ish scroll
    if (dx < -60) setMobileOpen(false);
  }, [mobileOpen]);

  return html`
    <div class=${"xmc-h-app " + (focusMode ? "nb-focus-mode" : "")}>
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
        <span class="xmc-h-mobheader__brand">
          <${Icon} name="ClawMark" className="xmc-h-mobheader__claw" />
          ${brand}
        </span>
      </header>

      ${mobileOpen
        ? html`<button type="button" class="xmc-h-overlay" onClick=${closeMobile} aria-label="close navigation"></button>`
        : null}

      <div class="xmc-h-shell-body">
        <aside
          id="app-sidebar"
          class=${"xmc-h-sidebar claw-sb " + (mobileOpen ? "is-mobile-open" : "")}
          aria-label="primary navigation"
          onTouchStart=${onSidebarTouchStart}
          onTouchEnd=${onSidebarTouchEnd}
        >
          <div class="claw-sb__brand">
            <span class="claw-sb__logo"><${Icon} name="ClawMark" className="claw-mark" /></span>
            <span class="claw-sb__wordmark">XMCLAW</span>
            <button
              type="button"
              class="claw-sb__close"
              onClick=${closeMobile}
              aria-label="close navigation"
            ><${Icon} name="X" /></button>
          </div>

          <button
            type="button"
            class="claw-sb__new"
            onClick=${() => { if (onNewSession) onNewSession(); navigateTo("/chat"); closeMobile(); }}
          ><${Icon} name="ClawMark" className="claw-mark-sm" /> 新对话</button>

          <${ContextStrip} status=${status} tokenUsage=${tokenUsage} />

          ${(() => {
            // 砍繁就简：主导航只留高频核心，其余收进「更多」。
            // 全部页面入口仍可达（功能不丢）。
            const ap = activePath === "/" ? "/dashboard" : activePath;
            const all = NAV_GROUPS.flatMap((g) => g.items);
            const PRIMARY = ["/chat", "/dashboard", "/memory", "/skills"];
            const prim = PRIMARY
              .map((p) => all.find((i) => i.path === p))
              .filter(Boolean);
            const rest = all.filter((i) => !PRIMARY.includes(i.path));
            const navItem = (i) => html`
              <a
                class=${"claw-nav-item" + (i.path === ap ? " is-active" : "")}
                href=${i.path}
                onClick=${(e) => { e.preventDefault(); navigateTo(i.path); closeMobile(); }}
              >
                <span class="claw-nav-item__ic"><${Icon} name=${i.icon} /></span>
                <span class="claw-nav-item__lbl">${i.label}</span>
                ${i.accent ? html`<span class="claw-nav-item__dot"></span>` : null}
              </a>`;
            return html`
              <nav class="claw-sb__nav" aria-label="primary navigation">
                ${prim.map(navItem)}
                <details class="claw-sb__more" open=${rest.some((i) => i.path === ap) || null}>
                  <summary class="claw-nav-item claw-nav-item--more">
                    <span class="claw-nav-item__ic"><${Icon} name="Sparkles" /></span>
                    <span class="claw-nav-item__lbl">更多</span>
                    <span class="claw-nav-item__chev">▾</span>
                  </summary>
                  <div class="claw-sb__more-list">${rest.map(navItem)}</div>
                </details>
              </nav>`;
          })()}

          <div class="claw-sb__spacer"></div>

          <${SidebarSystemActions} token=${token} />
          <div class="claw-sb__footrow">
            <${ThemeSwitcher} dropUp=${true} />
            <${LanguageSwitcher} />
          </div>
          <${SidebarFooter} />
        </aside>

        <div class="xmc-h-content-area">
          <${AppHeader}
            activePath=${activePath === "/" ? "/sessions" : activePath}
            onToggleComm=${toggleComm}
            onToggleNotif=${toggleNotif}
            onToggleFocus=${toggleFocus}
            focusMode=${focusMode}
            commOnline=${true}
          />
          ${(() => {
            // ClawHUD — signature "生命体征" telemetry bar, shown on every
            // page below the header. Fed by /api/v2/status.telemetry
            // (via useDaemonStatus). Heartbeat: alive once status loads,
            // idle while未连. Numbers default to 0/50 until the first poll.
            const tel = (status && status.telemetry) || {};
            return html`<${ClawHUD}
              status=${status ? "alive" : "idle"}
              memoryFacts=${tel.memory_facts || 0}
              skillCount=${tel.skill_count || 0}
              skillPending=${tel.skill_pending || 0}
              autonomy=${tel.autonomy != null ? tel.autonomy : 50}
            />`;
          })()}
          <main class="xmc-h-main" role="main">
            <${SetupBanner} token=${token} />
            ${children}
          </main>
        </div>
      </div>
      ${commOpen ? html`<${CommPanel} onClose=${() => setCommOpen(false)} token=${token} />` : null}
      ${notifOpen ? html`<${NotificationPanel} onClose=${() => setNotifOpen(false)} token=${token} />` : null}
      <${BuddyMascot} />
    </div>
  `;
}
