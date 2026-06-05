// XMclaw — Badge atom
//
// Small status / count pill. Used in TopBar, Sidebar items, status bar, …
//
// Props:
//   tone    "purple" | "cyan" | "green" | "amber" | "red" | "info" | "success" | "warn" | "error" | "muted"
//           (default: info)
//   children text content
//   aria-label pass-through when the tone conveys semantic meaning

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

const TONE_MAP = {
  purple: "nb-badge--purple",
  cyan: "nb-badge--cyan",
  green: "nb-badge--green",
  amber: "nb-badge--amber",
  red: "nb-badge--red",
  info: "nb-badge--purple",
  success: "nb-badge--green",
  warn: "nb-badge--amber",
  error: "nb-badge--red",
  muted: "nb-badge--cyan",
};

export function Badge(props) {
  const { tone = "info", class: cls = "", children, ...rest } = props;

  const className = ["nb-badge", TONE_MAP[tone] || TONE_MAP.info, cls]
    .filter(Boolean)
    .join(" ");

  return html`
    <span ...${rest} class=${className}>${children}</span>
  `;
}
