// XMclaw — Skeleton atom (enhanced)
//
// Pulsing placeholder for loading states. Replaces bare "加载中…" text
// with a shimmer block that hints at the eventual layout.
//
// Variants:
//   text    – single line (short / long via width prop)
//   title   – thicker, shorter line
//   circle  – avatar-style circle
//   card    – rectangular block
//
// Props:
//   variant  "text" | "title" | "circle" | "card"  (default: text)
//   width    string – custom width for text variant (e.g. "60%", "90%")
//   lines    number – for text variant, how many lines to render
//   height   string – custom height override
//   class    string – extra class names

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

export function Skeleton({
  variant = "text",
  width,
  lines = 3,
  height,
  class: cls = "",
  ...rest
}) {
  const baseClass = ["nb-skeleton", cls].filter(Boolean).join(" ");

  if (variant === "circle") {
    return html`
      <div ...${rest} class="${baseClass} nb-skeleton-circle" style=${height ? `height:${height};width:${height}` : ""} aria-hidden="true"></div>
    `;
  }

  if (variant === "card") {
    return html`
      <div ...${rest} class="${baseClass} nb-skeleton-card" style=${height ? `height:${height}` : ""} aria-hidden="true"></div>
    `;
  }

  if (variant === "title") {
    return html`
      <div ...${rest} class="${baseClass} nb-skeleton-title" style=${width ? `width:${width}` : ""} aria-hidden="true"></div>
    `;
  }

  // text variant (default)
  const textWidth = width || "100%";
  return html`
    <div ...${rest} aria-hidden="true" style="display:flex;flex-direction:column;gap:.5rem;">
      ${Array.from({ length: lines }, (_, i) => {
        const isLast = i === lines - 1;
        const w = isLast && !width ? "60%" : textWidth;
        const sizeClass = w === "60%" ? "short" : w === "90%" ? "long" : "";
        return html`
          <div
            key=${i}
            class="nb-skeleton nb-skeleton-text ${sizeClass}"
            style=${height ? `height:${height}` : ""}
          ></div>
        `;
      })}
    </div>
  `;
}
