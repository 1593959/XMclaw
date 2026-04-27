// XMclaw — CodeBlock molecule (port of hermes Markdown.tsx CodeBlock)
//
// Visual:
//   ┌─ lang badge ────────── copy button ┐
//   │                                    │
//   │  monospace lines, dark bg          │
//   └────────────────────────────────────┘
//
// hljs syntax-highlight loaded from esm.sh on first render. The
// language packs are lazy-loaded per code block (one extra fetch per
// new language seen). If the network is offline or hljs fails to
// load, we fall back to plain monospace — no broken UI.

const { h } = window.__xmc.preact;
const { useState, useEffect, useRef } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { toast } from "../../lib/toast.js";

// hljs core + per-language modules cached at module scope so a chat
// transcript with 30 code blocks doesn't refetch each time.
let _hljsCore = null;
let _hljsCorePromise = null;
const _hljsLangs = new Map();        // lang -> Promise<module|null>

const HLJS_VERSION = "11.10.0";
const HLJS_CDN_BASE = `https://esm.sh/highlight.js@${HLJS_VERSION}`;
const HLJS_CSS_HREF = `https://esm.sh/highlight.js@${HLJS_VERSION}/styles/atom-one-dark.css`;

// One-shot inject of the hljs theme stylesheet.
let _hljsCssInjected = false;
function _ensureHljsCss() {
  if (_hljsCssInjected) return;
  _hljsCssInjected = true;
  try {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = HLJS_CSS_HREF;
    document.head.appendChild(link);
  } catch (_) { /* SSR-safe */ }
}

async function _getHljsCore() {
  if (_hljsCore !== null) return _hljsCore;
  if (_hljsCorePromise) return _hljsCorePromise;
  _ensureHljsCss();
  _hljsCorePromise = import(`${HLJS_CDN_BASE}/lib/core`)
    .then((m) => {
      _hljsCore = m.default || m;
      return _hljsCore;
    })
    .catch((e) => {
      console.warn("[xmc] hljs core load failed", e);
      _hljsCore = false;
      return null;
    });
  return _hljsCorePromise;
}

// hljs per-language registration — lazy import via esm.sh, register
// against the core. Returns null when unavailable so callers fall
// back to plain text.
async function _getHljsLang(lang) {
  if (!lang) return null;
  const key = String(lang).toLowerCase().trim();
  if (!key) return null;
  if (_hljsLangs.has(key)) return _hljsLangs.get(key);
  const promise = (async () => {
    const core = await _getHljsCore();
    if (!core) return null;
    try {
      const m = await import(`${HLJS_CDN_BASE}/lib/languages/${key}`);
      const def = m.default || m;
      try {
        core.registerLanguage(key, def);
      } catch (_) { /* already registered or bad shape */ }
      return def;
    } catch (e) {
      // 404 etc → unknown language; that's fine, plain text it is.
      return null;
    }
  })();
  _hljsLangs.set(key, promise);
  return promise;
}

export function CodeBlock({ code, lang }) {
  const [copied, setCopied] = useState(false);
  const codeRef = useRef(null);

  // Run hljs once mount completes. We highlight imperatively (set
  // innerHTML) instead of mapping tokens to spans because hljs's
  // own emitter is the only sane way to keep its theme CSS in sync.
  useEffect(() => {
    let cancelled = false;
    const node = codeRef.current;
    if (!node) return;
    (async () => {
      const core = await _getHljsCore();
      if (cancelled || !core) return;
      try {
        let result;
        if (lang) {
          await _getHljsLang(lang);
          if (core.getLanguage(lang)) {
            result = core.highlight(code, { language: lang, ignoreIllegals: true });
          } else {
            result = core.highlightAuto(code);
          }
        } else {
          result = core.highlightAuto(code);
        }
        if (cancelled) return;
        node.innerHTML = result.value;
        node.classList.add("hljs");
      } catch (e) {
        // fall back to plain text rendering already in DOM
      }
    })();
    return () => { cancelled = true; };
  }, [code, lang]);

  const onCopy = async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(code);
      } else {
        // Fallback for non-secure contexts.
        const ta = document.createElement("textarea");
        ta.value = code;
        ta.setAttribute("readonly", "");
        ta.style.position = "absolute";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch (e) {
      toast.error("复制失败：" + (e.message || e));
    }
  };

  const langLabel = (lang || "").trim() || "text";

  return html`
    <div class="xmc-h-codeblock">
      <div class="xmc-h-codeblock__chrome">
        <span class="xmc-h-codeblock__lang">${langLabel}</span>
        <button
          type="button"
          class=${"xmc-h-codeblock__copy " + (copied ? "is-copied" : "")}
          onClick=${onCopy}
          title="复制代码"
        >${copied ? "已复制 ✓" : "复制"}</button>
      </div>
      <pre class="xmc-h-codeblock__pre"><code ref=${codeRef}>${code}</code></pre>
    </div>
  `;
}
