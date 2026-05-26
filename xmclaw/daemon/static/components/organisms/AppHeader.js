// XMclaw — AppHeader (top bar with breadcrumb + page actions)
//
// Desktop-only header that sits above the main content pane.
// Shows the current page title and optional action buttons.

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

export function AppHeader({ activePath, children }) {
  const meta = findPageMeta(activePath);
  return html`
    <header class="xmc-h-appheader" aria-label="page header">
      <div class="xmc-h-appheader__left">
        ${meta
          ? html`
            <span style="opacity:.45;font-size:.7rem">${meta.group}</span>
            <span style="opacity:.3">/</span>
            <span class="xmc-h-appheader__title">${meta.label}</span>
          `
          : html`<span class="xmc-h-appheader__title">XMclaw</span>`}
      </div>
      <div class="xmc-h-appheader__right">
        ${children}
      </div>
    </header>
  `;
}
