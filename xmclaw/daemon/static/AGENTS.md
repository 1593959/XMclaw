# AGENTS.md — `xmclaw/daemon/static/`

## 1. 职责（Responsibility）

This directory is the **single source of truth for the XMclaw Web UI** —
the Phase-0-through-Phase-6 build specified in
[`docs/FRONTEND_DESIGN.md`](../../../docs/FRONTEND_DESIGN.md) and tracked
as **Epic #23** in [`docs/DEV_ROADMAP.md`](../../../docs/DEV_ROADMAP.md).
It owns:

- the HTML entry (`index.html`), framework bootstrap (`bootstrap.js`),
  app root (`app.js`), router + store;
- all atoms / molecules / organisms / pages under `components/`;
- the design-token system under `styles/`;
- the vendor fallback bundle under `vendor/`.

It does not own backend routing: the daemon mounts this folder at `/ui/`
via `StaticFiles`, but any new REST/WS endpoint belongs in
`xmclaw/daemon/routers/`.

## 2. 依赖规则（Dependency rules）

This is a **pure-static browser bundle**, not Python. The dependency
rules are therefore expressed in terms of runtime imports inside the JS
modules:

- ✅ MAY import from: local relative paths within this tree,
  `https://esm.sh/preact@10`, `https://esm.sh/htm@3`, and the vendored
  `./vendor/preact.min.js` / `./vendor/htm.min.js` fallbacks.
- ❌ MUST NOT import from: any other CDN (no jQuery, no React, no Lodash,
  no Tailwind, no Google Fonts). ADR-001 / ADR-002 lock this down —
  adding a new CDN needs a fresh ADR.
- ❌ MUST NOT introduce a Node.js build step. The whole point is
  "edit + refresh" development.

The daemon side (`xmclaw/daemon/app.py`) MAY mount this folder via
`StaticFiles`. No other Python module should read the files here.

## 3. 测试入口（How to test changes here）

- **Unit**: `tests/unit/test_v2_ui_scaffold.py` — file-existence,
  HTML/bootstrap wiring, mount-point reachability, 500-line budget.
- **Manual smoke**:
  ```
  xmclaw start
  # open http://127.0.0.1:8765/ui/
  ```
  The shell should render with `bootstrap: cdn` (or `vendor` when
  offline). Click each sidebar item — the placeholder page should swap
  in without a full reload, and the URL should update.
- **Smart-gate lane**: `ui` (extend in `scripts/test_lanes.yaml` when
  Phase 1 adds Playwright e2e).

## 4. 禁止事项（Hard no's）

- ❌ Don't introduce a Node.js build step (Vite / webpack / Rollup /
  esbuild). CLAUDE.md and ADR-001 are explicit.
- ❌ Don't let any single JS or CSS file grow past **500 lines**
  (FRONTEND_DESIGN.md §1.4 — the Cline `ChatTextArea.tsx` 1622-line
  anti-example). Split into molecules / organisms before you hit it.
- ❌ Don't hard-code colours, spacing, or font sizes. Everything goes
  through `styles/tokens.css` so themes and `data-density="compact"`
  work uniformly.
- ❌ Don't skip `:focus-visible` on any interactive element. WCAG AA
  is non-negotiable per §10.1.
- ❌ Don't fetch anything from the public internet at runtime outside of
  the two ESM imports in `bootstrap.js`. Offline-first is a core value.

## 5. 关键文件（Key files / entry points）

- `index.html` — HTML entry; defines root div + stylesheet link order.
- `bootstrap.js` — CDN→vendor fallback resolver (ADR-009); sets
  `window.__xmc.{preact,htm,bootstrapSource}` before `app.js` loads.
- `app.js` — top-level Preact mount, Sidebar/TopBar/StatusBar shell,
  route table.
- `router.js` — `history.pushState`-based SPA router. Strips the
  `/ui` prefix so routes inside the app are `/chat`, `/skills`, …
- `store.js` — tiny pub/sub store; every slice lives here.
- `styles/tokens.css` — dual-layer design variables (XMclaw + VSCode
  fallback per ADR-003). Touch this file when adding a new theme or
  density mode.
- `components/atoms/` — the 5 Phase-0 atoms (Button / Badge / Icon /
  Avatar / Spinner) + shared `atoms.css`. Read before writing the first
  molecule in Phase 1.
- `vendor/` — self-hosted Preact + htm copies for offline / restricted
  environments. Populated on first install via `scripts/fetch_vendor.py`
  (Phase 0 deliverable); the directory is present in git but the `.min.js`
  files are gitignored.
