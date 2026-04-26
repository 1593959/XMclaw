// XMclaw — Hermes 6 themes 1:1 port (lib/themes/presets.ts).
//
// Theme switching applies these to :root via inline style. fontUrl is
// injected as a <link rel="stylesheet"> exactly once per URL — same
// dedup as Hermes ThemeProvider does internally.

const SYSTEM_SANS =
  'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const SYSTEM_MONO =
  'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace';

const DEFAULT_TYPO = {
  fontSans: SYSTEM_SANS,
  fontMono: SYSTEM_MONO,
  baseSize: "15px",
  lineHeight: "1.55",
  letterSpacing: "0",
};
const DEFAULT_LAYOUT = { radius: "0.5rem", density: "comfortable" };

export const THEMES = {
  default: {
    name: "default",
    label: "Hermes Teal",
    description: "Classic dark teal — the canonical Hermes look",
    palette: {
      background: { hex: "#041c1c", alpha: 1 },
      midground:  { hex: "#ffe6cb", alpha: 1 },
      foreground: { hex: "#ffffff", alpha: 0 },
      warmGlow: "rgba(255, 189, 56, 0.35)",
      noiseOpacity: 1,
    },
    typography: DEFAULT_TYPO,
    layout: DEFAULT_LAYOUT,
  },
  midnight: {
    name: "midnight",
    label: "Midnight",
    description: "Deep blue-violet with cool accents",
    palette: {
      background: { hex: "#0a0a1f", alpha: 1 },
      midground:  { hex: "#d4c8ff", alpha: 1 },
      foreground: { hex: "#ffffff", alpha: 0 },
      warmGlow: "rgba(167, 139, 250, 0.32)",
      noiseOpacity: 0.8,
    },
    typography: {
      fontSans: `"Inter", ${SYSTEM_SANS}`,
      fontMono: `"JetBrains Mono", ${SYSTEM_MONO}`,
      fontUrl:
        "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap",
      baseSize: "14px",
      lineHeight: "1.6",
      letterSpacing: "-0.005em",
    },
    layout: { radius: "0.75rem", density: "comfortable" },
  },
  ember: {
    name: "ember",
    label: "Ember",
    description: "Warm crimson and bronze — forge vibes",
    palette: {
      background: { hex: "#1a0a06", alpha: 1 },
      midground:  { hex: "#ffd8b0", alpha: 1 },
      foreground: { hex: "#ffffff", alpha: 0 },
      warmGlow: "rgba(249, 115, 22, 0.38)",
      noiseOpacity: 1,
    },
    typography: {
      fontSans: `"Spectral", Georgia, "Times New Roman", serif`,
      fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
      fontUrl:
        "https://fonts.googleapis.com/css2?family=Spectral:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;700&display=swap",
      baseSize: "15px",
      lineHeight: "1.6",
      letterSpacing: "0",
    },
    layout: { radius: "0.25rem", density: "comfortable" },
    colorOverrides: {
      destructive: "#c92d0f",
      warning: "#f97316",
    },
  },
  mono: {
    name: "mono",
    label: "Mono",
    description: "Clean grayscale — minimal and focused",
    palette: {
      background: { hex: "#0e0e0e", alpha: 1 },
      midground:  { hex: "#eaeaea", alpha: 1 },
      foreground: { hex: "#ffffff", alpha: 0 },
      warmGlow: "rgba(255, 255, 255, 0.1)",
      noiseOpacity: 0.6,
    },
    typography: {
      fontSans: `"IBM Plex Sans", ${SYSTEM_SANS}`,
      fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
      fontUrl:
        "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap",
      baseSize: "13px",
      lineHeight: "1.5",
      letterSpacing: "0",
    },
    layout: { radius: "0", density: "compact" },
  },
  cyberpunk: {
    name: "cyberpunk",
    label: "Cyberpunk",
    description: "Neon green on black — matrix terminal",
    palette: {
      background: { hex: "#040608", alpha: 1 },
      midground:  { hex: "#9bffcf", alpha: 1 },
      foreground: { hex: "#ffffff", alpha: 0 },
      warmGlow: "rgba(0, 255, 136, 0.22)",
      noiseOpacity: 1.2,
    },
    typography: {
      fontSans: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
      fontMono: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
      fontUrl:
        "https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=JetBrains+Mono:wght@400;700&display=swap",
      baseSize: "14px",
      lineHeight: "1.5",
      letterSpacing: "0.02em",
    },
    layout: { radius: "0", density: "compact" },
    colorOverrides: {
      success: "#00ff88",
      warning: "#ffd700",
      destructive: "#ff0055",
    },
  },
  rose: {
    name: "rose",
    label: "Rosé",
    description: "Soft pink and warm ivory — easy on the eyes",
    palette: {
      background: { hex: "#1a0f15", alpha: 1 },
      midground:  { hex: "#ffd4e1", alpha: 1 },
      foreground: { hex: "#ffffff", alpha: 0 },
      warmGlow: "rgba(249, 168, 212, 0.3)",
      noiseOpacity: 0.9,
    },
    typography: {
      fontSans: `"Fraunces", Georgia, serif`,
      fontMono: `"DM Mono", ${SYSTEM_MONO}`,
      fontUrl:
        "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=DM+Mono:wght@400;500&display=swap",
      baseSize: "16px",
      lineHeight: "1.7",
      letterSpacing: "0",
    },
    layout: { radius: "1rem", density: "spacious" },
  },
};

const DENSITY_MUL = { compact: 0.85, comfortable: 1, spacious: 1.2 };

const _injectedFonts = new Set();

function _injectFont(url) {
  if (!url || _injectedFonts.has(url)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = url;
  document.head.appendChild(link);
  _injectedFonts.add(url);
}

/** Apply a theme to :root inline style. Mirrors Hermes ThemeProvider's
 *  applyTheme — sets all CSS vars at once so a switch is one repaint. */
export function applyTheme(themeName) {
  const t = THEMES[themeName] || THEMES.default;
  const root = document.documentElement;
  const { palette, typography, layout, colorOverrides = {} } = t;

  const bg  = palette.background.hex;
  const mg  = palette.midground.hex;
  const fg  = palette.foreground.hex;
  const fga = palette.foreground.alpha;

  // 3-layer palette + warm glow + noise.
  root.style.setProperty("--background-base", bg);
  root.style.setProperty("--background-alpha", String(palette.background.alpha));
  root.style.setProperty(
    "--background",
    `color-mix(in srgb, ${bg} ${palette.background.alpha * 100}%, transparent)`
  );
  root.style.setProperty("--midground-base", mg);
  root.style.setProperty("--midground-alpha", String(palette.midground.alpha));
  root.style.setProperty(
    "--midground",
    `color-mix(in srgb, ${mg} ${palette.midground.alpha * 100}%, transparent)`
  );
  root.style.setProperty("--foreground-base", fg);
  root.style.setProperty("--foreground-alpha", String(fga));
  root.style.setProperty(
    "--foreground",
    `color-mix(in srgb, ${fg} ${fga * 100}%, transparent)`
  );
  root.style.setProperty("--warm-glow", palette.warmGlow);
  root.style.setProperty("--noise-opacity-mul", String(palette.noiseOpacity));

  // Typography.
  root.style.setProperty("--theme-font-sans", typography.fontSans);
  root.style.setProperty("--theme-font-mono", typography.fontMono);
  root.style.setProperty(
    "--theme-font-display",
    typography.fontDisplay || typography.fontSans
  );
  root.style.setProperty("--theme-base-size", typography.baseSize);
  root.style.setProperty("--theme-line-height", typography.lineHeight);
  root.style.setProperty("--theme-letter-spacing", typography.letterSpacing);

  // Layout (radius + density).
  root.style.setProperty("--theme-radius", layout.radius);
  root.style.setProperty("--radius", layout.radius);
  root.style.setProperty(
    "--theme-spacing-mul",
    String(DENSITY_MUL[layout.density] || 1)
  );
  root.style.setProperty(
    "--spacing",
    `calc(0.25rem * var(--theme-spacing-mul, 1))`
  );
  root.style.setProperty("--theme-density", layout.density);

  // colorOverrides — explicit shadcn-token pins.
  if (colorOverrides.destructive) {
    root.style.setProperty("--color-destructive", colorOverrides.destructive);
  }
  if (colorOverrides.warning) {
    root.style.setProperty("--color-warning", colorOverrides.warning);
  }
  if (colorOverrides.success) {
    root.style.setProperty("--color-success", colorOverrides.success);
  }

  // Optional external font sheet.
  if (typography.fontUrl) {
    _injectFont(typography.fontUrl);
  }

  // Persist + announce.
  try { localStorage.setItem("xmc_hermes_theme", themeName); } catch (_) {}
  root.dataset.hermesTheme = themeName;
}

export function readActiveTheme() {
  try {
    const v = localStorage.getItem("xmc_hermes_theme");
    if (v && THEMES[v]) return v;
  } catch (_) {}
  return "default";
}

export function listThemes() {
  return Object.values(THEMES).map((t) => ({
    name: t.name,
    label: t.label,
    description: t.description,
  }));
}

// Apply on module load so the LENS_0 → user-pinned theme transition is
// invisible (no flash of wrong palette).
applyTheme(readActiveTheme());
