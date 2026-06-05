// XMclaw — Keyboard Shortcuts Panel (molecule)
//
// Fixed-position modal that displays categorized keyboard shortcuts.
// Uses the Nebula design-system tokens (nb- prefix).
//
// Props:
//   open       boolean  – whether the panel is visible
//   onClose    function – called when the user dismisses the panel

const { h } = window.__xmc.preact;
const { useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

const SECTIONS = [
  {
    title: "Global",
    items: [
      { label: "命令面板", keys: ["⌘", "K"] },
      { label: "搜索", keys: ["⌘", "F"] },
      { label: "帮助", keys: ["?"] },
    ],
  },
  {
    title: "Chat",
    items: [
      { label: "发送", keys: ["↵"] },
      { label: "换行", keys: ["⇧", "↵"] },
      { label: "聚焦输入框", keys: ["/"] },
    ],
  },
  {
    title: "Navigation",
    items: [
      { label: "上一页", keys: ["⌥", "←"] },
      { label: "下一页", keys: ["⌥", "→"] },
    ],
  },
  {
    title: "System",
    items: [
      { label: "重启服务", keys: ["⌘", "⇧", "R"] },
      { label: "硬刷新", keys: ["⌘", "⇧", "⌥", "R"] },
      { label: "清除缓存", keys: ["⌘", "⇧", "⌫"] },
      { label: "DevTools", keys: ["⌘", "⇧", "I"] },
    ],
  },
  {
    title: "Communication",
    items: [
      { label: "切换连接", keys: ["⌘", "⇧", "C"] },
      { label: "发送 Ping", keys: ["⌘", "⇧", "P"] },
      { label: "重连 WS", keys: ["⌘", "⇧", "W"] },
    ],
  },
];

function _useEscToClose(active, onClose) {
  useEffect(() => {
    if (!active) return undefined;
    const handler = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose?.();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [active, onClose]);
}

export function KeyboardShortcutsPanel({ open, onClose }) {
  _useEscToClose(open, onClose);
  if (!open) return null;

  return html`
    <div
      class="nb-kb-panel show"
      role="dialog"
      aria-modal="true"
      aria-labelledby="nb-kb-title"
      onClick=${(e) => { if (e.target === e.currentTarget) onClose?.(); }}
    >
      <div class="nb-kb-box">
        <div class="nb-kb-header">
          <h3 id="nb-kb-title">键盘快捷键</h3>
          <button
            type="button"
            onClick=${() => onClose?.()}
            aria-label="关闭"
            title="关闭 (Esc)"
          >×</button>
        </div>
        <div class="nb-kb-body">
          ${SECTIONS.map((section) => html`
            <div class="nb-kb-section" key=${section.title}>
              <h4>${section.title}</h4>
              ${section.items.map((item) => html`
                <div class="nb-kb-row" key=${item.label}>
                  <span>${item.label}</span>
                  <div class="nb-kb-keys">
                    ${item.keys.map((k) => html`
                      <kbd class="nb-kb-key" key=${k}>${k}</kbd>
                    `)}
                  </div>
                </div>
              `)}
            </div>
          `)}
        </div>
      </div>
    </div>
  `;
}
