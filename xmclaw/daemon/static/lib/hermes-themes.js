// XMclaw — Hermes themes compatibility shim
//
// DEPRECATED: Use `lib/theme-engine.js` directly. This file re-exports
// the new theme engine so legacy imports continue to work during the
// transition period. It will be removed once all callers are updated.

export {
  THEMES,
  applyTheme,
  readActiveTheme,
  listThemes,
} from "./theme-engine.js";
