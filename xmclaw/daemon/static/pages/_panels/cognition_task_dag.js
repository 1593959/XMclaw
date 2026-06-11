// XMclaw — TaskDag panel for CognitionPage
//
// Extracted to keep Cognition.js under the 500-line UI budget.

const { h } = window.__xmc.preact;
const { useEffect, useState, useRef } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

// Phase 9 M1: Mermaid 改走共享 vendor 加载器(本地优先,CDN 兜底)。
import { loadMermaid } from "../../lib/vendor_loaders.js";

export function TaskDag({ data }) {
  const ref = useRef(null);
  const [svg, setSvg] = useState(null);

  useEffect(() => {
    let cancelled = false;
    async function render() {
      const mermaid = await loadMermaid();
      if (cancelled) return;

      const { nodes, edges } = data;
      if (!nodes.length) return;

      const lines = ["graph TD"];
      const statusShapes = {
        pending: "((",
        blocked: "((",
        running: "[[",
        completed: "([",
        failed: "{{",
        retrying: "{{",
        escalated: "{{",
      };
      const statusShapesEnd = {
        pending: "))",
        blocked: "))",
        running: "]]",
        completed: "])",
        failed: "}}",
        retrying: "}}",
        escalated: "}}",
      };
      const statusColors = {
        pending: "#888",
        blocked: "#f39c12",
        running: "#3498db",
        completed: "#2ecc71",
        failed: "#e74c3c",
        retrying: "#e67e22",
        escalated: "#9b59b6",
      };

      for (const n of nodes) {
        const id = n.id.replace(/[^a-zA-Z0-9]/g, "_");
        const label = n.label.replace(/"/g, '\\"');
        const start = statusShapes[n.status] || "((";
        const end = statusShapesEnd[n.status] || "))";
        const color = statusColors[n.status] || "#888";
        lines.push(`    ${id}${start}"${label}"${end}`);
        lines.push(`    style ${id} fill:${color}22,stroke:${color},stroke-width:2px`);
      }
      for (const e of edges) {
        const s = e.source.replace(/[^a-zA-Z0-9]/g, "_");
        const t = e.target.replace(/[^a-zA-Z0-9]/g, "_");
        lines.push(`    ${s} --> ${t}`);
      }

      const defn = lines.join("\n");
      try {
        const { svg: svgCode } = await mermaid.render("task-dag-" + Date.now(), defn);
        if (!cancelled) setSvg(svgCode);
      } catch (e) {
        if (!cancelled) setSvg(`<div style="color:var(--xmc-danger)">渲染失败: ${e.message}</div>`);
      }
    }
    render();
    return () => { cancelled = true; };
  }, [data]);

  useEffect(() => {
    if (ref.current && svg) {
      ref.current.innerHTML = svg;
    }
  }, [svg]);

  return html`<div ref=${ref} style="overflow:auto" />`;
}
