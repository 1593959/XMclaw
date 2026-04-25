// XMclaw — bootstrap
//
// Resolves Preact + htm using the dual-track strategy from
// docs/FRONTEND_DESIGN.md §11.3.1 (ADR-009):
//
//   1. CDN first  → https://esm.sh/preact@10 + https://esm.sh/htm@3
//   2. Vendor fallback → ./vendor/preact.min.js + ./vendor/htm.min.js
//   3. Forced local mode → ?assets=local query flag, or
//      localStorage.xmc_assets_mode === "local"
//
// The choice is memoised into localStorage.xmc_bootstrap_source so we can
// read it from a "mini DevTools" overlay later (§11.4).
//
// IMPORTANT: this file runs before ./app.js and before the tokens stylesheet
// finishes loading in some browsers. Keep it dependency-free.

const CDN_PREACT = "https://esm.sh/preact@10";
const CDN_HTM = "https://esm.sh/htm@3";
const VENDOR_PREACT = "./vendor/preact.min.js";
const VENDOR_HTM = "./vendor/htm.min.js";

const CDN_TIMEOUT_MS = 5000;

const statusEl = document.getElementById("xmc-bootstrap-status");

function setStatus(text, visible = false) {
  if (!statusEl) return;
  statusEl.textContent = text;
  statusEl.hidden = !visible;
}

function recordSource(source) {
  try {
    localStorage.setItem("xmc_bootstrap_source", source);
  } catch {
    // localStorage can throw in private-mode or iframe-sandboxed contexts.
    // Not fatal — the page still works.
  }
}

function resolveMode() {
  const params = new URLSearchParams(window.location.search);
  const query = params.get("assets");
  if (query === "local" || query === "cdn") return query;
  try {
    const stored = localStorage.getItem("xmc_assets_mode");
    if (stored === "local" || stored === "cdn") return stored;
  } catch {
    /* ignore */
  }
  return "auto";
}

function timeoutImport(url, ms) {
  // Promise.race leaves the loser dangling — if we don't cancel the
  // timer when the import resolves first, the rejected timeout still
  // fires later and spawns "Uncaught (in promise) timeout importing ..."
  // noise in the console (and, in pathological cases where the slow
  // CDN comes back after we already fell back to vendor, double-loads
  // app.js because two paths complete). Always clearTimeout on settle.
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`timeout importing ${url}`)),
      ms
    );
  });
  return Promise.race([import(url), timeout]).finally(() =>
    clearTimeout(timer)
  );
}

async function loadFromCdn() {
  const [preact, htmMod] = await Promise.all([
    timeoutImport(CDN_PREACT, CDN_TIMEOUT_MS),
    timeoutImport(CDN_HTM, CDN_TIMEOUT_MS),
  ]);
  return { preact, htmMod, source: "cdn" };
}

async function loadFromVendor() {
  const [preact, htmMod] = await Promise.all([
    import(VENDOR_PREACT),
    import(VENDOR_HTM),
  ]);
  return { preact, htmMod, source: "vendor" };
}

async function resolveFramework() {
  const mode = resolveMode();

  if (mode === "local") {
    return loadFromVendor();
  }
  if (mode === "cdn") {
    return loadFromCdn();
  }

  // auto: try CDN, fall back on any failure.
  try {
    return await loadFromCdn();
  } catch (cdnErr) {
    console.warn("[xmc] CDN load failed, using vendor", cdnErr);
    try {
      return await loadFromVendor();
    } catch (vendorErr) {
      // Both paths failed. Render a plain-HTML error so the user is not
      // staring at a blank page. In practice the only way to hit this is:
      //   - vendor files missing AND
      //   - CDN unreachable
      // Which means the install is broken.
      setStatus(
        "XMclaw: failed to load Preact from both CDN and vendor. " +
          "Check xmclaw/daemon/static/vendor/ or network.",
        true
      );
      throw vendorErr;
    }
  }
}

const frame = await resolveFramework();
recordSource(frame.source);

// Hand framework to app.js via window — app.js is a plain module so it
// imports these names rather than relying on globals for hot-module-style
// reuse, but we keep a window handle for mini-devtools later.
window.__xmc = window.__xmc || {};
window.__xmc.preact = frame.preact;
window.__xmc.htm = frame.htmMod.default || frame.htmMod;
window.__xmc.bootstrapSource = frame.source;

// Dynamic import so app.js only parses once the framework exists. This also
// lets app.js use top-level `window.__xmc.*` references without a race.
await import("./app.js");
