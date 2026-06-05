// XMclaw — AppHeader (top bar with breadcrumb + page actions)
//
// Modified by Worker F (2026-06-05):
//   - Added comm status button with online indicator (.nb-header-btn)
//   - Added notification bell button (.nb-header-btn)
//   - Added focus mode toggle button (.nb-header-btn)
//   - Buttons use native title attribute for accessible tooltips
//   - Kept existing activePath + children props interface

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

import { NAV_GROUPS, Icon } from "./AppShell.js";

function findPageMeta(path) {
  for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (item.path === path) {
        return { group: group.label, label: item.label, icon: item.icon };
      }
    }
  }
  return null;
}

export function AppHeader({ activePath, children, onToggleComm, onToggleNotif, onToggleFocus, focusMode, commOnline }) {
  const meta = findPageMeta(activePath);
  return html`
    <header class="xmc-h-appheader nb-header" aria-label="page header">
      <div class="xmc-h-appheader__left nb-header__left">
        ${meta
          ? html`
            <span style="opacity:.45;font-size:.7rem">${meta.group}</span>
            <span style="opacity:.3">/</span>
            <span class="xmc-h-appheader__title nb-header__title">${meta.label}</span>
          `
          : html`<span class="xmc-h-appheader__title nb-header__title">XMclaw</span>`}
      </div>
      <div class="xmc-h-appheader__right nb-header__right">
        <button
          type="button"
          class="nb-header-btn"
          onClick=${onToggleComm}
          title="通讯状态"
        >
          <span
            class="nb-status-dot"
            style=${commOnline
              ? ""
              : "background:var(--nb-error);box-shadow:0 0 8px rgba(239,68,68,0.4);animation:none;"}
          ></span>
        </button>
        <button
          type="button"
          class="nb-header-btn"
          onClick=${onToggleNotif}
          title="通知中心"
        >
          🔔
        </button>
        <button
          type="button"
          class="nb-header-btn"
          onClick=${onToggleFocus}
          title=${focusMode ? "退出专注模式" : "专注模式"}
        >
          🔦
        </button>
        ${children}
      </div>
    </header>
  `;
}
