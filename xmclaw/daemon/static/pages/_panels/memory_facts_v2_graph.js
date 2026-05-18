// Memory v2 — graph viz sub-panel (Phase 5c).
//
// Renders the L1 fact graph as a force-directed network using
// vis-network (loaded lazily from esm.sh CDN, with vendor fallback).
// Consumes the /api/v2/memory/v2/graph endpoint built in Phase 5a.
//
// Visual encoding (matches §9.3 of design doc):
//   * Node shape per kind   — preference/decision/identity color-coded
//   * Edge color per relation — CONTRADICTS red / SUPERSEDES dashed /
//     CAUSED_BY blue / PART_OF purple / SAME_TOPIC light / REFERS_TO green
//   * Node size scales with evidence_count (more evidence = bigger)
//
// Interaction:
//   * Drag — force-directed re-layout
//   * Hover — node tooltip with full text + meta
//   * Click — emit onFocusFact(fact_id) so parent shows detail drawer
//
// Failure modes handled:
//   * vis-network CDN unreachable → red banner with manual install hint
//   * empty graph → friendly empty state, not blank canvas

const { h } = window.__xmc.preact;
const { useState, useEffect, useRef, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet, apiPost } from "../../lib/api.js";


// ── Color tables (kept in sync with FactsV2Tab) ──────────────────


const KIND_COLOR = {
  preference: "#5a8ed6",
  decision: "#9e6ed4",
  identity: "#5cb37d",
  commitment: "#d4a35c",
  correction: "#d05a5a",
  project: "#d68b3f",
  episode: "#8d8d8d",
  event: "#4a4a4a",  // L0 event pseudo-id nodes
};

const RELATION_COLOR = {
  CONTRADICTS: { color: "#d05a5a", width: 2 },
  SUPERSEDES: { color: "#888", width: 2, dashes: [5, 5] },
  CAUSED_BY: { color: "#5a8ed6", width: 2 },
  PART_OF: { color: "#9e6ed4", width: 2 },
  REFERS_TO: { color: "#5cb37d", width: 1.5 },
  SAME_TOPIC: { color: "#d4d4d4", width: 1 },
};


// ── vis-network loader (lazy + cached) ───────────────────────────


let _visModule = null;
let _visPromise = null;

async function _loadVisNetwork() {
  if (_visModule) return _visModule;
  if (_visPromise) return _visPromise;
  _visPromise = (async () => {
    try {
      const mod = await import("https://esm.sh/vis-network@9.1.9/standalone");
      _visModule = mod;
      return mod;
    } catch (err) {
      _visPromise = null;
      throw new Error(
        "vis-network CDN unreachable. Add npm vendor copy under "
        + "static/vendor/ to enable offline use.",
      );
    }
  })();
  return _visPromise;
}


// ── Truncate helper ──────────────────────────────────────────────


function _truncate(s, n = 60) {
  if (!s) return "";
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}


// ── Main component ───────────────────────────────────────────────


export function FactsGraphView({ token, focusFactId, onFocusFact }) {
  const containerRef = useRef(null);
  const networkRef = useRef(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [stats, setStats] = useState({ nodes: 0, edges: 0 });

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const vis = await _loadVisNetwork();
      const params = new URLSearchParams();
      if (focusFactId) {
        params.set("focus_fact_id", focusFactId);
        params.set("max_hops", "2");
      }
      params.set("limit", "100");
      const data = await apiGet(
        `/api/v2/memory/v2/graph?${params.toString()}`,
        token,
      );
      if (!data || !Array.isArray(data.nodes)) {
        setError("graph endpoint returned bad payload");
        setLoading(false);
        return;
      }

      // Map nodes → vis-network shape.
      const visNodes = data.nodes.map((n) => {
        const color = KIND_COLOR[n.kind] || "#777";
        const evidence = n.evidence_count || 1;
        const size = Math.min(40, 12 + evidence * 4);
        const isEvent = n.kind === "event";
        return {
          id: n.id,
          label: _truncate(n.text || n.id, 28),
          title: [
            `[${n.kind}/${n.scope || "?"}]`,
            n.text || n.id,
            n.confidence != null ? `conf ${n.confidence.toFixed(2)}` : "",
            n.evidence_count != null ? `evidence ${n.evidence_count}` : "",
            n.layer ? `layer ${n.layer}` : "",
          ].filter(Boolean).join("\n"),
          color: {
            background: color,
            border: color,
            highlight: { background: color, border: "#fff" },
          },
          shape: isEvent ? "diamond" : "dot",
          size,
          font: { color: "#eee", size: 11 },
        };
      });

      const visEdges = data.edges.map((e) => {
        const style = RELATION_COLOR[e.relation] || { color: "#999", width: 1 };
        return {
          id: e.id,
          from: e.source,
          to: e.target,
          label: e.relation,
          title: `${e.relation} (strength ${e.strength.toFixed(2)})`,
          color: { color: style.color, highlight: "#fff" },
          width: style.width,
          dashes: style.dashes || false,
          arrows: "to",
          font: {
            size: 9,
            color: "#aaa",
            strokeWidth: 0,
            align: "middle",
          },
          smooth: { type: "continuous" },
        };
      });

      // Destroy any prior network instance before re-creating.
      if (networkRef.current) {
        networkRef.current.destroy();
        networkRef.current = null;
      }

      if (containerRef.current && visNodes.length > 0) {
        const network = new vis.Network(
          containerRef.current,
          {
            nodes: new vis.DataSet(visNodes),
            edges: new vis.DataSet(visEdges),
          },
          {
            physics: {
              enabled: true,
              solver: "forceAtlas2Based",
              forceAtlas2Based: {
                gravitationalConstant: -40,
                centralGravity: 0.005,
                springLength: 120,
                damping: 0.5,
              },
              stabilization: { iterations: 150 },
            },
            interaction: {
              hover: true,
              tooltipDelay: 200,
              navigationButtons: false,
            },
            edges: { smooth: false },
          },
        );
        // Click on a node → propagate fact_id up so parent loads detail.
        network.on("click", (params) => {
          if (params.nodes.length === 0) return;
          const fid = params.nodes[0];
          if (typeof fid === "string" && !fid.startsWith("event:")) {
            if (onFocusFact) onFocusFact(fid);
          }
        });
        networkRef.current = network;
      }
      setStats({ nodes: visNodes.length, edges: visEdges.length });
    } catch (err) {
      setError(err?.message || String(err));
    } finally {
      setLoading(false);
    }
  }, [focusFactId, token, onFocusFact]);

  // Refresh on mount + when focusFactId changes.
  useEffect(() => {
    refresh();
    return () => {
      if (networkRef.current) {
        networkRef.current.destroy();
        networkRef.current = null;
      }
    };
  }, [refresh]);

  return html`
    <section style="margin-top:.6rem">
      <header style="display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin-bottom:.4rem">
        <strong>知识图谱</strong>
        ${focusFactId
          ? html`<span style="font-size:.78rem;color:var(--xmc-fg-muted)">焦点: <code>${focusFactId.slice(0, 32)}…</code></span>`
          : html`<span style="font-size:.78rem;color:var(--xmc-fg-muted)">总览模式</span>`}
        <button
          type="button"
          class="xmc-h-btn"
          onClick=${refresh}
          style="font-size:.78rem;padding:.3rem .6rem"
        >🔄 刷新</button>
        <button
          type="button"
          class="xmc-h-btn"
          onClick=${async () => {
            try {
              const r = await apiPost(
                "/api/v2/memory/v2/relink_same_topic", {}, token,
              );
              const d = (r && r.ok && (r.data || r)) || r || {};
              alert(
                "重建关系完成\n扫描: " + (d.scanned || 0) +
                "\n新增边: " + (d.edges_added || 0) +
                "\n跳过(已存在): " + (d.edges_skipped || 0),
              );
              refresh();
            } catch (e) {
              alert("重建关系失败: " + (e && e.message || e));
            }
          }}
          title="按 Wave-32+ 新规则（跨 kind + 共享实体桥接）扫一遍所有事实，补上漏链的 SAME_TOPIC 边"
          style="font-size:.78rem;padding:.3rem .6rem"
        >🔗 重建关系</button>
        ${focusFactId
          ? html`<button
              type="button"
              class="xmc-h-btn"
              onClick=${() => onFocusFact && onFocusFact(null)}
              style="font-size:.78rem;padding:.3rem .6rem"
            >← 回总览</button>`
          : null}
        ${stats.nodes > 0
          ? html`<span style="font-size:.72rem;color:var(--xmc-fg-muted);margin-left:auto">${stats.nodes} 节点 / ${stats.edges} 边</span>`
          : null}
      </header>

      ${error
        ? html`
            <div class="xmc-h-error" role="alert" style="padding:.7rem .9rem;border:1px solid var(--color-destructive);border-radius:6px;background:color-mix(in srgb, var(--color-destructive) 8%, transparent)">
              <strong>图谱加载失败</strong>
              <div style="font-size:.85rem;margin-top:.3rem">${error}</div>
            </div>
          `
        : null}

      ${loading && !error
        ? html`<div style="opacity:.7;padding:1rem 0">加载图谱中（首次会从 CDN 拉 vis-network ~250KB）…</div>`
        : null}

      <div
        ref=${containerRef}
        class="xmc-mem-graph"
        style="width:100%;height:560px;border:1px solid var(--color-border);border-radius:6px;background:#0a1218;position:relative;${(loading || error || stats.nodes === 0) ? 'display:none' : ''}"
      ></div>

      ${!loading && !error && stats.nodes === 0
        ? html`
            <div style="border:1px dashed var(--color-border);border-radius:6px;padding:2rem;text-align:center;color:var(--xmc-fg-muted)">
              暂无事实。先在"列表"视图加几条事实，再回来看图谱。
            </div>
          `
        : null}

      <details style="margin-top:.6rem">
        <summary style="cursor:pointer;font-size:.78rem;color:var(--xmc-fg-muted)">视觉编码图例</summary>
        <div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-top:.4rem;font-size:.78rem">
          <div>
            <strong>节点 (kind)</strong>
            <ul style="margin:.3rem 0 0;padding-left:1.2rem;list-style:none">
              ${Object.entries(KIND_COLOR).map(([k, c]) => html`
                <li key=${k} style="display:flex;align-items:center;gap:.4rem">
                  <span style=${`width:.7rem;height:.7rem;background:${c};display:inline-block;border-radius:50%`}></span>
                  <span>${k}</span>
                </li>
              `)}
            </ul>
          </div>
          <div>
            <strong>边 (relation)</strong>
            <ul style="margin:.3rem 0 0;padding-left:1.2rem;list-style:none">
              ${Object.entries(RELATION_COLOR).map(([r, s]) => html`
                <li key=${r} style="display:flex;align-items:center;gap:.4rem">
                  <span style=${`width:1.3rem;height:0;border-top:${s.width}px ${s.dashes ? 'dashed' : 'solid'} ${s.color};display:inline-block`}></span>
                  <span>${r}</span>
                </li>
              `)}
            </ul>
          </div>
        </div>
      </details>
    </section>
  `;
}
