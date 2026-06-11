// XMclaw — CodeBlock molecule (Nebula v2)
//
// Visual:
//   ┌─ lang label ────────── copy / download buttons ┐
//   │ 1  │  code...                                    │
//   │ 2  │  code...                                    │
//   └──────────────────────────────────────────────────┘
//
// hljs syntax-highlight loaded from esm.sh on first render. The
// language packs are lazy-loaded per code block. If the network is
// offline or hljs fails to load, we fall back to plain monospace.

const { h } = window.__xmc.preact;
const { useState, useEffect, useRef } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { toast } from "../../lib/toast.js";

// hljs core + per-language modules cached at module scope so a chat
// transcript with 30 code blocks doesn't refetch each time.
let _hljsCore = null;
let _hljsCorePromise = null;
const _hljsLangs = new Map();        // lang -> Promise<module|null>

let _dompurify = null;
let _dompurifyPromise = null;
async function _getDomPurify() {
  if (_dompurify) return _dompurify;
  if (_dompurifyPromise) return _dompurifyPromise;
  _dompurifyPromise = (async () => {
    try {
      const mod = await import("https://esm.sh/dompurify@3");
      _dompurify = mod.default;
      return _dompurify;
    } catch (e) {
      console.warn("[xmc] DOMPurify load failed for CodeBlock", e);
      return null;
    }
  })();
  return _dompurifyPromise;
}

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

// Wave-27 fix-15 (2026-05-16): pseudo-language tags that callers
// commonly emit but hljs has no module for.
const _HLJS_NOOP_LANGS = new Set([
  "text", "txt", "plain", "plaintext", "output",
  "raw", "log", "none", "tty",
]);

// hljs per-language registration — lazy import via esm.sh, register
// against the core. Returns null when unavailable so callers fall
// back to plain text.
async function _getHljsLang(lang) {
  if (!lang) return null;
  const key = String(lang).toLowerCase().trim();
  if (!key) return null;
  if (_HLJS_NOOP_LANGS.has(key)) return null;
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

export function CodeBlock({ code, lang, maxLines = 20 }) {
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const codeRef = useRef(null);

  const lines = code.split("\n");
  const shouldFold = lines.length > maxLines && maxLines > 0;
  const displayCode = (shouldFold && !expanded)
    ? lines.slice(0, maxLines).join("\n") + "\n"
    : code;
  const displayLines = displayCode.split("\n");
  // Remove trailing empty line created by the fold suffix if present
  if (displayLines.length > 1 && displayLines[displayLines.length - 1] === "") {
    displayLines.pop();
  }
  const lineNumbers = displayLines.map((_, i) => i + 1).join("\n");

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
        // H3 fix: sanitise hljs output through DOMPurify before injecting
        // into innerHTML. hljs is generally safe (entity-escaped spans),
        // but highlightAuto may misclassify code containing HTML-like
        // fragments; defence-in-depth.
        const purify = await _getDomPurify();
        const safeHtml = purify
          ? purify.sanitize(result.value, { ALLOWED_TAGS: ["span"], ALLOWED_ATTR: ["class"] })
          : result.value;
        try {
          node.innerHTML = safeHtml;
        } catch (_e) {
          node.textContent = code; // Last-resort fallback
        }
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

  const onDownload = () => {
    try {
      const blob = new Blob([code], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `snippet.${lang || "txt"}`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error("下载失败：" + (e.message || e));
    }
  };

  const langLabel = (lang || "").trim() || "text";

  return html`
    <div class="nb-codeblock-wrap">
      <div class="nb-codeblock-header">
        <span class="nb-codeblock-lang">${langLabel}</span>
        <div class="nb-codeblock-actions">
          ${shouldFold
            ? html`
                <button
                  type="button"
                  onClick=${() => setExpanded((v) => !v)}
                  title=${expanded ? "折叠代码" : "展开代码"}
                >${expanded ? "折叠 ↑" : `展开 ↓ (${lines.length - maxLines} 行)`}</button>
              `
            : null}
          <button type="button" onClick=${onDownload} title="下载">⬇ 下载</button>
          <button type="button" onClick=${onCopy} title="复制">${copied ? "✓ 已复制" : "📋 复制"}</button>
        </div>
      </div>
      <div class="nb-codeblock-v2">
        <div class="nb-codeblock-lines">${lineNumbers}</div>
        <pre class="nb-codeblock-code"><code ref=${codeRef}>${displayCode}</code></pre>
      </div>
    </div>
  `;
}
