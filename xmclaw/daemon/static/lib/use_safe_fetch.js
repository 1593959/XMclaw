// XMclaw — useSafeFetch hook (Audit pass-3 finding B6).
//
// Drops the boilerplate from every page's "load + setState on mount"
// pattern AND fixes the silent setState-on-unmounted-component bug
// that 8 pages had pre-this-helper:
//
//   useEffect(() => {
//     apiGet(URL, token).then(setX);     // <-- if user navigates
//   }, [token]);                          //     before fetch settles,
//                                          //     setX fires on a dead
//                                          //     component → console
//                                          //     warning + GC pressure
//
// useSafeFetch wraps the same call in an isMounted guard:
//
//   useSafeFetch(URL, token, setX);     // <-- same shape, no leak
//
// Returns ``{loading, error, refresh}`` so callers can render their own
// loading / error states without re-implementing the state machine.
//
// Per the 2026-05-09 standing rule (CLAUDE.md), this lives in the lib/
// directory so the front-back tests can exercise it via TestClient
// integration with whatever page consumes it.

const { useState, useEffect, useRef, useCallback } = window.__xmc.preact_hooks;

import { apiGet, apiSend } from "./api.js";

/**
 * useSafeFetch — auto-cancellable apiGet hook.
 *
 * @param url — URL to fetch (string OR null/undefined to skip).
 * @param token — bearer token for apiGet.
 * @param setData — callback invoked with the parsed body when fetch
 *                  succeeds AND the component is still mounted.
 * @param deps — extra dependencies that should trigger a re-fetch
 *               (default: ``[url, token]``). Pass an empty array
 *               to fetch only on mount.
 *
 * @returns ``{loading, error, refresh}``.
 *   - ``loading``: true while a fetch is in flight.
 *   - ``error``: the error object (with ``.body`` / ``.status`` from
 *     api.js) when the most recent fetch failed; null otherwise.
 *   - ``refresh()``: re-fire the fetch on demand (e.g. after a POST
 *     mutates server state).
 */
export function useSafeFetch(url, token, setData, deps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const isMountedRef = useRef(true);

  const fetchOnce = useCallback(async () => {
    if (!url || !token) return;
    setLoading(true);
    setError(null);
    try {
      const body = await apiGet(url, token);
      if (isMountedRef.current) {
        setData(body);
      }
    } catch (e) {
      if (isMountedRef.current) {
        setError(e);
      }
    } finally {
      if (isMountedRef.current) {
        setLoading(false);
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, token, ...(deps || [])]);

  useEffect(() => {
    isMountedRef.current = true;
    fetchOnce();
    return () => {
      isMountedRef.current = false;
    };
  }, [fetchOnce]);

  return { loading, error, refresh: fetchOnce };
}

/**
 * useSafePost — fires an apiPost (or any apiSend) on demand, with the
 * same isMounted guard. Returns ``{run, loading, error, lastResult}``.
 *
 * Usage:
 *   const { run: install, loading } = useSafePost(token);
 *   <button onClick={() => install("POST", "/api/v2/skills/install", {id})}>
 *     Install
 *   </button>
 */
export function useSafePost(token) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [lastResult, setLastResult] = useState(null);
  const isMountedRef = useRef(true);

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const run = useCallback(
    async (method, url, body) => {
      if (!token) {
        const err = new Error("token required");
        if (isMountedRef.current) setError(err);
        return { ok: false, error: err };
      }
      if (isMountedRef.current) {
        setLoading(true);
        setError(null);
      }
      try {
        const result = await apiSend(method, url, body, token);
        if (isMountedRef.current) {
          setLastResult(result);
          setLoading(false);
        }
        return { ok: true, result };
      } catch (e) {
        if (isMountedRef.current) {
          setError(e);
          setLoading(false);
        }
        return { ok: false, error: e };
      }
    },
    [token],
  );

  return { run, loading, error, lastResult };
}
