// XMclaw — Lightbox molecule
//
// Re-exports the singleton-based lightbox system from lib/lightbox.js
// so consumers can import the viewport and imperative helpers from the
// components layer as well.
//
// The underlying lib has been updated to use nb- prefixed CSS classes
// (nb-lightbox, nb-lightbox__close, …) to align with the Nebula
// design system.

export {
  LightboxViewport,
  openLightbox,
  closeLightbox,
} from "../../lib/lightbox.js";

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

/** Declarative wrapper that mounts the singleton viewport. */
export function Lightbox() {
  // The lib viewport is self-managing; this component just ensures
  // it is present in the tree when used declaratively.
  return html`<${LightboxViewport} />`;
}
