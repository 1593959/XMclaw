// XMclaw — Button atom
//
// Spec: FRONTEND_DESIGN.md §5 (atoms).
//
// Props:
//   variant  "primary" | "secondary" | "ghost" | "danger"   (default: primary)
//   size     "sm" | "md" | "lg"                              (default: md)
//   disabled boolean
//   type     native <button type>                            (default: button)
//   onClick  handler
//   aria-label / aria-describedby / etc. pass through
//
// The exported symbol is a Preact function component — bootstrap.js must
// have resolved Preact + htm onto window.__xmc before this module loads.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

export function Button(props) {
  const {
    variant = "primary",
    size = "md",
    disabled = false,
    type = "button",
    class: cls = "",
    children,
    ...rest
  } = props;

  const className = [
    "xmc-btn",
    `xmc-btn--${variant}`,
    size !== "md" ? `xmc-btn--${size}` : "",
    cls,
  ]
    .filter(Boolean)
    .join(" ");

  return html`
    <button
      ...${rest}
      class=${className}
      type=${type}
      disabled=${disabled || null}
      aria-disabled=${disabled ? "true" : null}
    >
      ${children}
    </button>
  `;
}
