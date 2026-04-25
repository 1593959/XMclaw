// XMclaw — Spinner atom
//
// Indeterminate progress indicator. `label` is required for a11y — it
// becomes the accessible name and is read on focus / when a screen reader
// encounters the element. The visual label next to the spin can be hidden
// via `hideLabel`.
//
// Props:
//   label       string (required)
//   hideLabel   boolean — show label to sighted users? (default false)
//   size        "sm" | "md" | "lg"                  (default md)

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

export function Spinner(props) {
  const {
    label,
    hideLabel = false,
    size = "md",
    class: cls = "",
    ...rest
  } = props;

  const className = [
    "xmc-spinner",
    size === "sm" ? "xmc-spinner--sm" : "",
    size === "lg" ? "xmc-spinner--lg" : "",
    cls,
  ]
    .filter(Boolean)
    .join(" ");

  return html`
    <span
      ...${rest}
      class=${className}
      role="status"
      aria-live="polite"
      aria-label=${hideLabel ? label : null}
    >
      <span class="xmc-spinner__dot" aria-hidden="true"></span>
      ${hideLabel ? null : html`<span>${label}</span>`}
    </span>
  `;
}
