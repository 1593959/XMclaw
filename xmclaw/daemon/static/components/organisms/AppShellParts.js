// XMclaw — AppShell sub-components (B-323 split).
//
// Lifted out of AppShell.js to keep that file under the 500-line UI
// budget (FRONTEND_DESIGN.md §1.4). The sidebar tree (NavLink /
// NavRoot / NavGroup), the footer chrome (ThemeSwitcher /
// LanguageSwitcher / SidebarFooter / SidebarSystemActions) and the
// daemon-status / cost strip live here. ICONS map + NAV_GROUPS stay
// in AppShell.js because the v2 ui_scaffold test reads icon coverage
// from that file.
//
// ESM circular import note: this file imports Icon + NAV_GROUPS +
// helpers from ../AppShell.js, and AppShell.js imports several of
// our exports back. ESM handles the cycle because every cross-edge
// is consumed inside a function body (lazy at call time), not at
// module-init time.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import {
  Icon,
  NAV_GROUPS,
  readCollapsedGroups,
  persistCollapsedGroups,
} from "./AppShell.js";
import { apiGet } from "../../lib/api.js";
import { confirmDialog } from "../../lib/dialog.js";
import {
  THEMES, listThemes, applyTheme, readActiveTheme,
} from "../../lib/theme-engine.js";


export function NavLink({ item, active, onClick }) {
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
export function NavRoot({ activePath, onItemClick }) {
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
        style="display:flex;align-items:center;gap:.4rem;width:100%;padding:.4rem .5rem;background:transparent;border:0;color:var(--xmc-fg-muted);font-size:.72rem;letter-spacing:.03em;font-weight:500;cursor:pointer;border-radius:4px"
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


export function ThemeSwitcher({ dropUp }) {
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


export function LanguageSwitcher() {
  const { useState, useEffect } = window.__xmc.preact_hooks;
  const [open, setOpen] = useState(false);
  const [locale, setLocaleState] = useState(() => {
    try { return localStorage.getItem("xmc_locale") || "zh_CN"; }
    catch { return "zh_CN"; }
  });

  const locales = [
    { code: "zh_CN", label: "简体中文" },
    { code: "en",    label: "English" },
  ];

  const pick = (code) => {
    try { localStorage.setItem("xmc_locale", code); } catch {}
    setLocaleState(code);
    setOpen(false);
    // Notify i18n subscribers
    try {
      const evt = new Event("xmc_locale_change");
      window.dispatchEvent(evt);
    } catch {}
  };

  useEffect(() => {
    const onClickOutside = (e) => {
      if (!e.target.closest(".xmc-h-langsw")) setOpen(false);
    };
    window.addEventListener("click", onClickOutside);
    return () => window.removeEventListener("click", onClickOutside);
  }, []);

  const active = locales.find((l) => l.code === locale) || locales[0];
  return html`
    <div class="xmc-h-langsw" style="position:relative">
      <button
        type="button"
        class="xmc-h-langsw__btn"
        title="language"
        onClick=${(e) => { e.stopPropagation(); setOpen((v) => !v); }}
        style="display:flex;align-items:center;gap:.3rem;background:transparent;border:0;color:var(--xmc-fg-muted);cursor:pointer;font-size:.75rem;padding:.2rem .4rem;border-radius:4px"
      >
        <${Icon} name="Globe" />
        <span>${active.label}</span>
      </button>
      ${open
        ? html`
          <ul
            style="position:absolute;bottom:120%;right:0;min-width:120px;background:var(--xmc-bg-elevated);border:1px solid var(--color-border);border-radius:6px;padding:.3rem 0;list-style:none;margin:0;box-shadow:0 4px 12px rgba(0,0,0,.3);z-index:200"
          >
            ${locales.map((l) => html`
              <li
                key=${l.code}
                onClick=${() => pick(l.code)}
                style="padding:.4rem .8rem;cursor:pointer;font-size:.78rem;color:${l.code === locale ? 'var(--xmc-accent)' : 'inherit'};background:${l.code === locale ? 'var(--xmc-bg-hover)' : 'transparent'}"
                onMouseEnter=${(e) => { e.currentTarget.style.background = 'var(--xmc-bg-hover)'; }}
                onMouseLeave=${(e) => { e.currentTarget.style.background = l.code === locale ? 'var(--xmc-bg-hover)' : 'transparent'; }}
              >
                ${l.label}
              </li>
            `)}
          </ul>
        `
        : null}
    </div>
  `;
}


export function SidebarFooter() {
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


export function SidebarSystemActions({ token }) {
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


export function useDaemonStatus(token) {
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


export function ContextStrip({ status, tokenUsage }) {
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
