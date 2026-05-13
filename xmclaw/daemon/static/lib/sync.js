// XMclaw — UI state cross-device sync client (Wave 13).
//
// Tiny client for /api/v2/sync/ui-state. Push your settings on
// change, pull them on boot. Conflict policy: last write wins (no
// vector clocks, no CRDT — single user single tap is the expected
// shape).
//
// Usage:
//
//   import { fetchUiState, putUiState, patchUiState, debouncedPatch } from "./sync.js";
//
//   // Boot:
//   const remote = await fetchUiState(token);
//   if (remote.updated_ts > localUpdatedTs()) {
//     applyRemote(remote.state);
//   } else {
//     await putUiState(localSnapshot(), token);  // push our newer state
//   }
//
//   // On a single-setting change:
//   debouncedPatch({ theme: "dark" }, token);
//
// Single source of truth for the merge math lives in the consumer —
// this module is just transport. Keeps testing simple and lets a
// future Wave 18 multi-user version swap stores without rewriting the
// caller.

import { apiGet, apiSend } from "./api.js";

const ENDPOINT = "/api/v2/sync/ui-state";

export async function fetchUiState(token) {
  return apiGet(ENDPOINT, token);
}

export async function putUiState(state, token) {
  return apiSend("PUT", ENDPOINT, { state }, token);
}

export async function patchUiState(patch, token) {
  return apiSend("PATCH", ENDPOINT, { state: patch }, token);
}

// Debounce coalesces rapid-fire patches into one network round-trip.
// Each call replaces the pending key set; on the trailing edge we
// send the cumulative diff. 500ms covers "user spamming the model
// picker dropdown" without holding the network long enough to lose
// state on a tab close (Wave 13 doesn't yet flush on beforeunload —
// add if needed).
const DEBOUNCE_MS = 500;
let _pending = null;
let _timer = null;

export function debouncedPatch(patch, token) {
  if (!patch || typeof patch !== "object") return;
  _pending = { ..._pending, ...patch };
  if (_timer) clearTimeout(_timer);
  _timer = setTimeout(async () => {
    const flush = _pending;
    _pending = null;
    _timer = null;
    try {
      await patchUiState(flush, token);
    } catch (e) {
      // Surface to console for diagnosis but don't crash the caller —
      // sync is best-effort, local state remains correct.
      // eslint-disable-next-line no-console
      console.warn("[xmc/sync] patch failed", e);
    }
  }, DEBOUNCE_MS);
}

// Force-flush any pending debounced patch immediately (e.g. before
// tab close, route change, or test teardown).
export async function flushPending(token) {
  if (_timer) {
    clearTimeout(_timer);
    _timer = null;
  }
  if (_pending) {
    const flush = _pending;
    _pending = null;
    await patchUiState(flush, token);
  }
}

export const _internals = {
  DEBOUNCE_MS,
  // exposed for test-only inspection; do not rely on these in app
  // code.
  hasPending: () => !!_pending,
};
