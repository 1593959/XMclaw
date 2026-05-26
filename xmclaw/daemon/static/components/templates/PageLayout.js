// XMclaw — PageLayout template
//
// Unified page wrapper used by non-Chat pages. Provides consistent
// title bar, subtitle, action slot, and content scroll area.
//
// Usage:
//   <${PageLayout} title="设置" subtitle="管理模型、语音与安全">
//     <${SettingsContent} ... />
//   </${PageLayout}>

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

export function PageLayout({ title, subtitle, actions, children }) {
  return html`
    <div class="xmc-page">
      <header class="xmc-page__header">
        <div class="xmc-page__title-group">
          <h1 class="xmc-page__title">${title}</h1>
          ${subtitle
            ? html`<p class="xmc-page__subtitle">${subtitle}</p>`
            : null}
        </div>
        ${actions
          ? html`<div class="xmc-page__actions">${actions}</div>`
          : null}
      </header>
      <div class="xmc-page__body">
        ${children}
      </div>
    </div>
  `;
}
