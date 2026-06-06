// XMclaw — CanvasArtifact
//
// Renders an agent-generated visual artifact inline in a message bubble.
// Supported kinds: mermaid, html, svg, chart, table.
//
// Each artifact is wrapped in a collapsible <details> (same pattern as
// ToolCard) so the transcript doesn't get bloated by large visuals.

const { h } = window.__xmc.preact;
const { useEffect, useRef, useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

// ── lazy Mermaid loader (shared with cognition_task_dag.js) ───────
let _mermaidPromise = null;
function loadMermaid() {
  if (!_mermaidPromise) {
    _mermaidPromise = import(
      "https://esm.sh/mermaid@10/dist/mermaid.esm.min.mjs"
    ).then((m) => {
      m.default.initialize({ startOnLoad: false, theme: "dark" });
      return m.default;
    });
  }
  return _mermaidPromise;
}

// ── lazy Chart.js loader ──────────────────────────────────────────
let _chartPromise = null;
function loadChartJs() {
  if (!_chartPromise) {
    _chartPromise = import(
      "https://esm.sh/chart.js@4/auto?standalone"
    ).then((m) => m.default);
  }
  return _chartPromise;
}

// ── kind renderers ────────────────────────────────────────────────

export function MermaidView({ content }) {
  const ref = useRef(null);
  const [svg, setSvg] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let cancelled = false;
    async function render() {
      try {
        const mermaid = await loadMermaid();
        if (cancelled) return;
        const { svg: svgCode } = await mermaid.render(
          "mermaid-" + Date.now(),
          content
        );
        if (!cancelled) setSvg(svgCode);
      } catch (e) {
        if (!cancelled) setErr(String(e));
      }
    }
    render();
    return () => {
      cancelled = true;
    };
  }, [content]);

  if (err) {
    return html`
      <div class="xmc-canvas-error">
        <small>图表渲染失败：${err}</small>
        <pre class="xmc-canvas-raw">${content}</pre>
      </div>
    `;
  }
  if (svg === null) {
    return html`<div class="xmc-canvas-loading">正在渲染图表…</div>`;
  }
  return html`<div
    class="xmc-canvas-mermaid"
    ref=${ref}
    dangerouslySetInnerHTML=${{ __html: svg }}
  />`;
}

function HtmlView({ content }) {
  return html`<iframe
    class="xmc-canvas-html"
    srcdoc=${`<!doctype html>
<html><head><meta charset=utf-8>
<style>
body{background:#1a1f2a;color:#e0e4ea;font:14px/1.5 system-ui,sans-serif;margin:12px}
</style></head><body>${content}</body></html>`}
    sandbox="allow-scripts"
    title="artifact"
  />`;
}

function SvgView({ content }) {
  return html`<div
    class="xmc-canvas-svg"
    dangerouslySetInnerHTML=${{ __html: content }}
  />`;
}

function ChartView({ content }) {
  const canvasRef = useRef(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let cancelled = false;
    let chart = null;
    async function render() {
      try {
        const Chart = await loadChartJs();
        if (cancelled) return;
        const cfg = JSON.parse(content);
        const ctx = canvasRef.current.getContext("2d");
        chart = new Chart(ctx, cfg);
      } catch (e) {
        if (!cancelled) setErr(String(e));
      }
    }
    render();
    return () => {
      cancelled = true;
      if (chart) chart.destroy();
    };
  }, [content]);

  if (err) {
    return html`
      <div class="xmc-canvas-error">
        <small>Chart error: ${err}</small>
        <pre class="xmc-canvas-raw">${content}</pre>
      </div>
    `;
  }
  return html`<canvas ref=${canvasRef} class="xmc-canvas-chart" />`;
}

function TableView({ content }) {
  let data;
  try {
    data = JSON.parse(content);
  } catch {
    data = { headers: [], rows: [] };
  }
  const headers = data.headers || [];
  const rows = data.rows || [];

  return html`
    <table class="xmc-canvas-table">
      <thead>
        <tr>
          ${headers.map((h) => html`<th>${h}</th>`)}
        </tr>
      </thead>
      <tbody>
        ${rows.map(
          (r) => html`
            <tr>
              ${(Array.isArray(r) ? r : []).map((c) => html`<td>${c}</td>`)}
            </tr>
          `
        )}
      </tbody>
    </table>
  `;
}

// ── main component ────────────────────────────────────────────────

// Crisp inline SVG glyphs (16px, currentColor) — replaces the emoji set
// which rendered inconsistently across platforms and looked toy-like.
const KIND_GLYPHS = {
  // flow / diagram nodes
  mermaid:
    '<path d="M4 4h5v4H4zM11 12h5v4h-5zM4 16h5v4H4zM6.5 8v4m0 0h7m-7 4v-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
  // globe
  html: '<circle cx="10" cy="10" r="7" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M3 10h14M10 3c2.5 2.5 2.5 11.5 0 14M10 3c-2.5 2.5-2.5 11.5 0 14" fill="none" stroke="currentColor" stroke-width="1.4"/>',
  // vector pen
  svg: '<path d="M4 16l8-8 2-2 2 2-2 2-8 8-3 1z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>',
  // bar chart
  chart:
    '<path d="M4 17V8M9.3 17V4M14.6 17v-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  // grid
  table:
    '<rect x="3.5" y="4.5" width="13" height="11" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M3.5 8.5h13M8.5 4.5v11" stroke="currentColor" stroke-width="1.3"/>',
};

function KindIcon({ kind }) {
  const glyph = KIND_GLYPHS[kind] || KIND_GLYPHS.mermaid;
  return html`<svg
    class="xmc-canvas-artifact__glyph"
    viewBox="0 0 20 20"
    width="16"
    height="16"
    aria-hidden="true"
    dangerouslySetInnerHTML=${{ __html: glyph }}
  />`;
}

function ToolbarButton({ label, title, onClick }) {
  return html`<button
    type="button"
    class="xmc-canvas-artifact__act"
    title=${title}
    onClick=${(e) => {
      e.preventDefault();
      e.stopPropagation();
      onClick();
    }}
  >
    ${label}
  </button>`;
}

export function CanvasArtifact({ artifact }) {
  const { kind, title, content } = artifact;
  const [copied, setCopied] = useState(false);

  let body;
  switch (kind) {
    case "mermaid":
      body = html`<${MermaidView} content=${content} />`;
      break;
    case "html":
      body = html`<${HtmlView} content=${content} />`;
      break;
    case "svg":
      body = html`<${SvgView} content=${content} />`;
      break;
    case "chart":
      body = html`<${ChartView} content=${content} />`;
      break;
    case "table":
      body = html`<${TableView} content=${content} />`;
      break;
    default:
      body = html`<pre class="xmc-canvas-raw">${content}</pre>`;
  }

  const copySource = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch (e) {
      /* clipboard blocked — ignore */
    }
  };

  const download = () => {
    const ext =
      kind === "mermaid"
        ? "mmd"
        : kind === "svg"
        ? "svg"
        : kind === "html"
        ? "html"
        : kind === "chart" || kind === "table"
        ? "json"
        : "txt";
    const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${(title || "artifact").replace(/[^\w.-]+/g, "_")}.${ext}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return html`
    <details
      class="xmc-canvas-artifact"
      data-kind=${kind}
      open=${artifact.open !== false}
    >
      <summary class="xmc-canvas-artifact__summary">
        <span class="xmc-canvas-artifact__chev" aria-hidden="true">▸</span>
        <span class="xmc-canvas-artifact__icon" aria-hidden="true">
          <${KindIcon} kind=${kind} />
        </span>
        <span class="xmc-canvas-artifact__title">${title}</span>
        <span class="xmc-canvas-artifact__kind">${kind}</span>
        <span class="xmc-canvas-artifact__tools">
          <${ToolbarButton}
            label=${copied ? "已复制" : "复制源码"}
            title="复制原始内容到剪贴板"
            onClick=${copySource}
          />
          <${ToolbarButton}
            label="下载"
            title="下载为文件"
            onClick=${download}
          />
        </span>
      </summary>
      <div class="xmc-canvas-artifact__body">${body}</div>
    </details>
  `;
}
