# XMclaw Web UI

## Quick Start (Legacy)

Just open `index.html` directly in a browser. No build required.

## Modern Development

```bash
cd web
npm install
npm run dev      # Dev server with hot reload at http://localhost:5173
npm run build    # Production build to dist/
```

## Architecture

```
web/
├── index.html          # Entry HTML (no build needed)
├── main_new.js        # Legacy monolithic JS (used without build)
├── styles.css         # Legacy stylesheet
├── src/               # Modern ES modules (requires build)
│   ├── modules/
│   │   ├── state.js   # Centralized state management
│   │   └── i18n.js   # Internationalization
│   └── main.js       # Module-based entry
├── package.json       # Node dependencies
├── vite.config.js     # Vite build config
└── dist/              # Production build output
```

## Migration Guide

The legacy `main_new.js` will continue to work without any build.
The `src/` directory is for gradual migration to a modular architecture.

To migrate a component:
1. Create a module under `src/modules/`
2. Import it from `src/main.js`
3. Update HTML to use `dist/main.js` instead of `main_new.js`
