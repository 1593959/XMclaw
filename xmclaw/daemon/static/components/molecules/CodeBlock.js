// XMclaw — CodeBlock molecule (port of hermes Markdown.tsx CodeBlock)
//
// Visual:
//   ┌─ lang badge ────────── copy button ┐
//   │                                    │
//   │  monospace lines, dark bg          │
//   └────────────────────────────────────┘
//
// We don't pull in highlight.js — keeping the no-build promise. The
// dark bg + scrollable mono pre is a faithful render of Hermes's
// CodeBlock visual at the structural level. hljs can be added later
// via esm.sh if/when the user OK's the kbyte cost.

const { h } = window.__xmc.preact;
const { useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { toast } from "../../lib/toast.js";

export function CodeBlock({ code, lang }) {
  const [copied, setCopied] = useState(false);
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
      <pre class="xmc-h-codeblock__pre"><code>${code}</code></pre>
    </div>
  `;
}
