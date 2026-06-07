// XMclaw — Theme Engine v2
//
// Unifies legacy themes with the new ds-* semantic token system.
//
// Switching themes updates BOTH:
//   1. data-theme attribute on <html>  → drives --ds-* via CSS selectors
//   2. Inline the reference CSS variables      → keeps existing components working
//
// The 6 the themes (default/midnight/ember/mono/cyberpunk/rose)
// continue to work exactly as before. The 3 new ds themes (dark/light/teal)
// add data-theme coverage so the new token layer responds automatically.

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

/* ── Theme Definitions ──────────────────────────────────────────── */

export const THEMES = {
  /* -- New ds-aware themes (get data-theme attribute) -- */
  dark: {
    name: "dark",
    label: "Dark",
    description: "Professional dark grey with indigo accents",
    palette: {
      background: { hex: "#0a0a0f", alpha: 1 },
      midground:  { hex: "#7c8cff", alpha: 1 },
      foreground: { hex: "#e8e8ef", alpha: 0 },
      warmGlow: "rgba(124, 140, 255, 0.25)",
      noiseOpacity: 0.8,
    },
    typography: DEFAULT_TYPO,
    layout: DEFAULT_LAYOUT,
  },
  light: {
    name: "light",
    label: "Light",
    description: "Clean white with indigo accents",
    palette: {
      background: { hex: "#ffffff", alpha: 1 },
      midground:  { hex: "#4f5dff", alpha: 1 },
      foreground: { hex: "#1a1a24", alpha: 0 },
      warmGlow: "rgba(79, 93, 255, 0.15)",
      noiseOpacity: 0.5,
    },
    typography: DEFAULT_TYPO,
    layout: DEFAULT_LAYOUT,
    colorOverrides: {
      destructive: "#ef4444",
      success: "#22c55e",
      warning: "#f59e0b",
    },
  },
  teal: {
    name: "teal",
    label: "Teal",
    description: "Classic dark teal — the original look",
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
  claw: {
    name: "claw",
    label: "Claw",
    description: "Deep space ink with electric lime accents",
    palette: {
      background: { hex: "#0a0b0f", alpha: 1 },
      midground:  { hex: "#c8ff4d", alpha: 1 },
      foreground: { hex: "#eaedf3", alpha: 0 },
      warmGlow: "rgba(200, 255, 77, 0.22)",
      noiseOpacity: 0.9,
    },
    typography: {
      fontSans: SYSTEM_SANS,
      fontMono: '"JetBrains Mono", ' + SYSTEM_MONO,
      baseSize: "14px",
      lineHeight: "1.62",
      letterSpacing: "0",
    },
    layout: { radius: "0.6rem", density: "comfortable" },
  },
  nebula: {
    name: "nebula",
    label: "Nebula",
    description: "Deep space with nebula purple and aurora cyan",
    palette: {
      background: { hex: "#0B0E14", alpha: 1 },
      midground:  { hex: "#8B5CF6", alpha: 1 },
      foreground: { hex: "#F1F5F9", alpha: 0 },
      warmGlow: "rgba(139, 92, 246, 0.25)",
      noiseOpacity: 0.85,
    },
    typography: {
      fontSans: SYSTEM_SANS,
      fontMono: '"JetBrains Mono", ' + SYSTEM_MONO,
      baseSize: "14px",
      lineHeight: "1.62",
      letterSpacing: "0",
    },
    layout: { radius: "0.75rem", density: "comfortable" },
  },

  /* -- Legacy themes (inline vars only, no data-theme) -- */
  default: {
    name: "default",
    label: "Classic Teal",
    description: "Classic dark teal — the canonical look",
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

const DS_THEME_NAMES = new Set(["dark", "light", "teal", "claw", "nebula"]);
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

/* ── Core API ───────────────────────────────────────────────────── */

/**
 * Apply a theme. Sets data-theme for ds-aware themes AND writes
 * the reference inline CSS variables for backwards compatibility.
 */
export function applyTheme(themeName) {
  const t = THEMES[themeName] || THEMES.teal;
  const root = document.documentElement;
  const { palette, typography, layout, colorOverrides = {} } = t;

  /* v2: data-theme for ds-* token system */
  if (DS_THEME_NAMES.has(themeName)) {
    root.dataset.theme = themeName;
  } else {
    // Legacy themes have no CSS selector coverage in design-system.css;
    // clear data-theme so the inline vars below take full effect.
    delete root.dataset.theme;
  }

  const bg  = palette.background.hex;
  const mg  = palette.midground.hex;
  const fg  = palette.foreground.hex;
  const fga = palette.foreground.alpha;

  /* 3-layer palette + warm glow + noise */
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

  /* Typography */
  root.style.setProperty("--theme-font-sans", typography.fontSans);
  root.style.setProperty("--theme-font-mono", typography.fontMono);
  root.style.setProperty(
    "--theme-font-display",
    typography.fontDisplay || typography.fontSans
  );
  root.style.setProperty("--theme-base-size", typography.baseSize);
  root.style.setProperty("--theme-line-height", typography.lineHeight);
  root.style.setProperty("--theme-letter-spacing", typography.letterSpacing);

  /* Layout */
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

  /* colorOverrides */
  if (colorOverrides.destructive) {
    root.style.setProperty("--color-destructive", colorOverrides.destructive);
  }
  if (colorOverrides.warning) {
    root.style.setProperty("--color-warning", colorOverrides.warning);
  }
  if (colorOverrides.success) {
    root.style.setProperty("--color-success", colorOverrides.success);
  }

  /* External font */
  if (typography.fontUrl) {
    _injectFont(typography.fontUrl);
  }

  /* Persist + announce */
  try { localStorage.setItem("xmc_hermes_theme", themeName); } catch (_) {}
  root.dataset.hermesTheme = themeName;
}

/** Read the persisted theme name (or fallback).
 *
 * Order: (1) the user's stored preference, (2) the data-theme baked
 * into index.html (the project default — e.g. "claw"), (3) "claw".
 * Pre-fix this fell straight back to "teal" AND ignored the baked
 * data-theme, so applyTheme(readActiveTheme()) on module load clobbered
 * index.html's default theme on every fresh browser — the "I set
 * data-theme=claw but it shows teal" bug. */
export function readActiveTheme() {
  try {
    const baked = document.documentElement.dataset.theme;
    if (baked && THEMES[baked]) return baked;
  } catch (_) {}
  try {
    const v = localStorage.getItem("xmc_hermes_theme");
    if (v && THEMES[v]) return v;
  } catch (_) {}
  return "claw";
}

/** List all available themes for UI pickers. */
export function listThemes() {
  return Object.values(THEMES).map((t) => ({
    name: t.name,
    label: t.label,
    description: t.description,
  }));
}

/* Apply on module load so the initial palette transition is invisible. */
applyTheme(readActiveTheme());
