// XMclaw — minimal streaming-safe markdown renderer
//
// Phase 1 ships a deliberately small parser. We do NOT pull in a 30 KB
// markdown library because:
//   1. The streaming case re-parses on every LLM_CHUNK — perf matters.
//   2. We need rendering that's safe to invoke on partial-token strings;
//      most libraries don't promise output stability when a fenced block
//      is half-arrived.
//
// Supported subset:
//   * Hard line breaks ("\n" → <br>)
//   * Fenced code blocks (```lang\n…\n```)
//   * Inline code (`x`)
//   * Bold (**text**) and italic (*text*) — not nested
//   * Plain auto-link (http(s)://… → <a>)
//
// Everything else passes through escaped. When Phase 2 lands the rich
// "tool card" UI, we'll graduate to `marked` from a CDN and DOMPurify; for
// now this keeps the surface trivially auditable.
//
// Output is a *string* of safe HTML — callers should set it via
// dangerouslySetInnerHTML on a Preact node or innerHTML on a DOM node.

const HTML_ESCAPE = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => HTML_ESCAPE[c]);
}

// Pull fenced ``` blocks out first so the inline pass doesn't accidentally
// "format" code. Returns { stripped, blocks } where placeholders look like
// `\u0000CB${i}\u0000` so they can't collide with user input.
function extractCodeBlocks(src) {
  const blocks = [];
  const stripped = src.replace(/```([a-zA-Z0-9_+-]*)\n([\s\S]*?)```/g, (m, lang, body) => {
    const idx = blocks.length;
    blocks.push({ lang: lang || "", body });
    return `\u0000CB${idx}\u0000`;
  });
  return { stripped, blocks };
}

function renderInline(s) {
  let out = escapeHtml(s);
  // Inline code: `text`
  out = out.replace(/`([^`\n]+)`/g, (_, code) => `<code class="xmc-md__code">${code}</code>`);
  // Bold then italic; intentionally simple, no nesting.
  out = out.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  out = out.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?]|$)/g, '$1<em>$2</em>');
  // Auto-link bare URLs.
  out = out.replace(
    /(https?:\/\/[^\s<>")\]]+[^\s<>")\].,;:!?])/g,
    (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`
  );
  return out;
}

export function renderMarkdown(src) {
  if (!src) return "";
  const { stripped, blocks } = extractCodeBlocks(String(src));
  // Treat the surviving text line-by-line; each line is inline-rendered.
  const lines = stripped.split("\n");
  const renderedLines = lines.map(renderInline);
  let out = renderedLines.join("<br>");
  // Splice fenced blocks back in.
  out = out.replace(/\u0000CB(\d+)\u0000/g, (_, i) => {
    const block = blocks[Number(i)];
    if (!block) return "";
    const langClass = block.lang ? ` data-lang="${escapeHtml(block.lang)}"` : "";
    return `<pre class="xmc-md__pre"${langClass}><code>${escapeHtml(block.body)}</code></pre>`;
  });
  return out;
}
