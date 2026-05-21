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

function MermaidView({ content }) {
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
        <small>Mermaid render error: ${err}</small>
        <pre class="xmc-canvas-raw">${content}</pre>
      </div>
    `;
  }
  if (svg === null) {
    return html`<div class="xmc-canvas-loading">Rendering diagram…</div>`;
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

const KIND_ICONS = {
  mermaid: "🧜",
  html: "🌐",
  svg: "🎨",
  chart: "📊",
  table: "📋",
};

export function CanvasArtifact({ artifact }) {
  const { artifact_id, kind, title, content } = artifact;
  const icon = KIND_ICONS[kind] || "📄";

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

  return html`
    <details class="xmc-canvas-artifact" open=${artifact.open !== false}>
      <summary class="xmc-canvas-artifact__summary">
        <span class="xmc-canvas-artifact__icon" aria-hidden="true"
          >${icon}</span
        >
        <span class="xmc-canvas-artifact__title">${title}</span>
        <span class="xmc-canvas-artifact__kind">${kind}</span>
      </summary>
      <div class="xmc-canvas-artifact__body">${body}</div>
    </details>
  `;
}
