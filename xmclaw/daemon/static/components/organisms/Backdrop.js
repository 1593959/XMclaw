// XMclaw — Backdrop 1:1 port of hermes-agent/web/src/components/Backdrop.tsx
//
// Replicates the visual layer stack of `<Overlays dark />` from
// `@nous-research/ui` with no leva/gsap/three peer deps.
//
//   z-1   bg = `var(--background-base)`, mix-blend-mode: difference
//   z-2   filler-bg jpeg, inverted, opacity 0.033, difference
//   z-99  warm top-left vignette (`var(--warm-glow)`), opacity 0.22, lighten
//   z-101 noise grain (SVG, ~55% opacity × `--noise-opacity-mul`,
//         color-dodge) — gated on GPU tier (skipped for prefers-reduced-motion)
//
// `useGpuTier` from @nous-research/ui returns 0 when WebGL is
// unavailable, the renderer is a software rasterizer, or the user has
// `prefers-reduced-motion: reduce` set. We check the same conditions
// directly here so the noise layer skips when appropriate.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

// Mirrors @nous-research/ui/hooks/use-gpu-tier shape. Returns 0 when
// the user has prefers-reduced-motion OR the page is in a sandboxed
// frame without WebGL — both conditions skip the animated noise layer.
function detectGpuTier() {
  try {
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      return 0;
    }
    const c = document.createElement("canvas");
    const gl =
      c.getContext("webgl2") ||
      c.getContext("webgl") ||
      c.getContext("experimental-webgl");
    if (!gl) return 0;
    const dbg = gl.getExtension("WEBGL_debug_renderer_info");
    const renderer = dbg
      ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || ""
      : "";
    // Same heuristic Nous DS uses — software rasterizers skip noise.
    if (/SwiftShader|llvmpipe|software/i.test(String(renderer))) return 0;
    return 1;
  } catch (_) {
    return 0;
  }
}

const NOISE_SVG_DATA_URI =
  "url(\"data:image/svg+xml,%3Csvg viewBox='0 0 512 512' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' fill='%23eaeaea' filter='url(%23n)' opacity='0.6'/%3E%3C/svg%3E\")";

export function Backdrop() {
  const [gpuTier, setGpuTier] = useState(0);

  // Detect once on mount — Nous DS does the same thing, no re-detection.
  useEffect(() => {
    setGpuTier(detectGpuTier());
  }, []);

  return html`
    <${BackdropZ1} />
    <${BackdropZ2} />
    <${BackdropZ99} />
    ${gpuTier > 0 ? html`<${BackdropZ101} />` : null}
  `;
}

// z-1: solid background-base @ mix-blend-mode: difference.
function BackdropZ1() {
  return html`
    <div
      aria-hidden="true"
      class="xmc-backdrop xmc-backdrop--z1"
    ></div>
  `;
}

// z-2: filler bg (default ds-asset; theme.assets.bg overrides).
function BackdropZ2() {
  return html`
    <div
      aria-hidden="true"
      class="xmc-backdrop xmc-backdrop--z2"
    >
      <img
        alt=""
        class="xmc-backdrop__filler theme-default-filler"
        fetchPriority="low"
        src="./ds-assets/filler-bg0.jpg"
      />
    </div>
  `;
}

// z-99: warm top-left vignette.
function BackdropZ99() {
  return html`
    <div
      aria-hidden="true"
      class="xmc-backdrop xmc-backdrop--z99"
    ></div>
  `;
}

// z-101: noise grain (gated on GPU tier).
function BackdropZ101() {
  return html`
    <div
      aria-hidden="true"
      class="xmc-backdrop xmc-backdrop--z101"
      style=${{
        backgroundImage: NOISE_SVG_DATA_URI,
        backgroundSize: "512px 512px",
        mixBlendMode: "color-dodge",
        opacity: "calc(0.55 * var(--noise-opacity-mul, 1))",
      }}
    ></div>
  `;
}
