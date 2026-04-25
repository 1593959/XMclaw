// XMclaw — minimal pub/sub store
//
// Designed for the app scale we expect (single-page, ~12 routes, one WS).
// No reducers, no middleware — just a `{ state, setState, subscribe }`
// primitive we can grow into a Zustand-style store once the Chat page lands
// in Phase 1.
//
// API contract:
//   createStore(initial)            → { getState, setState, subscribe }
//   getState()                      → frozen snapshot of current state
//   setState(patchOrFn)             → merge a patch OR apply a functional
//                                     updater; triggers all subscribers
//   subscribe(listener)             → () => unsubscribe
//
// The store exports `app` — the top-level store instance consumed by app.js.
// Every new slice (sessions, skills, …) appends onto the initial state here.

function freeze(obj) {
  // Object.freeze is shallow, which is what we want: deep freezing turns
  // trivial .push / .splice into throw-spaghetti inside subscribers.
  return Object.freeze(obj);
}

export function createStore(initial) {
  let state = freeze({ ...initial });
  const listeners = new Set();

  function getState() {
    return state;
  }

  function setState(patch) {
    const next =
      typeof patch === "function"
        ? { ...state, ...patch(state) }
        : { ...state, ...patch };
    state = freeze(next);
    for (const listener of listeners) {
      try {
        listener(state);
      } catch (err) {
        // Never let one subscriber blow up the store.
        console.error("[xmc] store listener threw", err);
      }
    }
  }

  function subscribe(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  return { getState, setState, subscribe };
}

// App-wide store. Add slices as features land.
export const app = createStore({
  // Router slice (router.js writes here).
  route: { path: "/chat", params: {} },

  // Session slice (Phase 1 will populate from the daemon session API + WS).
  session: { id: null, lifecycle: "idle" },

  // Connection slice (WS heartbeat).
  connection: { status: "disconnected", lastPing: null },

  // UI prefs (Phase 5 settings page will bind here).
  ui: {
    theme: "dark",
    density: "comfortable",
    locale: "zh-CN",
  },

  // Bootstrap diag (populated from window.__xmc.bootstrapSource).
  bootstrap: {
    source: window.__xmc ? window.__xmc.bootstrapSource : "unknown",
  },
});
