// XMclaw — Avatar atom
//
// Used for: brand mark in sidebar, user/assistant avatars in chat,
// skill-owner avatars in the evolution page.
//
// Props:
//   initials  fallback text when no image is supplied (2 chars max)
//   src       optional image URL
//   alt       alt text for the image (required when src is supplied)
//   size      "sm" | "md" | "lg"                              (default: md)

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

export function Avatar(props) {
  const {
    initials = "",
    src,
    alt = "",
    size = "md",
    class: cls = "",
    ...rest
  } = props;

  const className = ["xmc-avatar", size === "lg" ? "xmc-avatar--lg" : "", cls]
    .filter(Boolean)
    .join(" ");

  const trimmed = (initials || "").slice(0, 2).toUpperCase();

  return html`
    <span
      ...${rest}
      class=${className}
      role=${src ? "img" : null}
      aria-label=${src ? alt : null}
    >
      ${src
        ? html`<img src=${src} alt=${alt} />`
        : html`<span aria-hidden="true">${trimmed}</span>`}
    </span>
  `;
}
