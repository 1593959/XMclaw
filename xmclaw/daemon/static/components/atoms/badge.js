// XMclaw — Badge atom
//
// Small status / count pill. Used in TopBar, Sidebar items, status bar, …
//
// Props:
//   tone    "info" | "success" | "warn" | "error" | "muted"   (default: info)
//   children text content
//   aria-label pass-through when the tone conveys semantic meaning

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

export function Badge(props) {
  const { tone = "info", class: cls = "", children, ...rest } = props;

  const className = ["xmc-badge", `xmc-badge--${tone}`, cls]
    .filter(Boolean)
    .join(" ");

  return html`
    <span ...${rest} class=${className}>${children}</span>
  `;
}
