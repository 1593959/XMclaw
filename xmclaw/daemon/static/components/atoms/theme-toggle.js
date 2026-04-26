// XMclaw — ThemeToggle atom
//
// Small button cycling document.documentElement.dataset.theme between
// "dark", "light", and "high-contrast". Persists choice to localStorage
// so reload remembers. Mirrors open-webui Settings/Interface theme
// selector but reduced to a one-click toggle for the topbar.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

const THEMES = ["dark", "light", "high-contrast"];
const LS_KEY = "xmc_theme";

function readTheme() {
  try {
    const v = localStorage.getItem(LS_KEY);
    if (THEMES.includes(v)) return v;
  } catch (_) { /* ignore */ }
  return document.documentElement.dataset.theme || "dark";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(LS_KEY, theme); } catch (_) { /* ignore */ }
}

const GLYPHS = {
  dark: "🌙",
  light: "☀️",
  "high-contrast": "◐",
};

export function ThemeToggle() {
  const [theme, setTheme] = useState(readTheme());

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const onClick = () => {
    const idx = (THEMES.indexOf(theme) + 1) % THEMES.length;
    setTheme(THEMES[idx]);
  };

  return html`
    <button
      type="button"
      class="xmc-theme-toggle"
      aria-label=${"主题: " + theme + " (点击切换)"}
      title=${"主题: " + theme}
      onClick=${onClick}
    >
      <span aria-hidden="true">${GLYPHS[theme] || "◐"}</span>
    </button>
  `;
}
