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
  // Wave-32+ — LLM-named cluster head. Distinctive cyan so the
  // hierarchy is visible at a glance: topic nodes anchor groups
  // of normal fact nodes via PART_OF edges.
  topic: "#3aa8c4",
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

// Wave-32+ resilience: try multiple CDNs with per-CDN timeouts so a
// blocked / slow esm.sh doesn't hang the graph view indefinitely.
// User report (2026-05-19): "加载图谱中..." stuck forever because
// esm.sh wasn't reachable from their network. Pre-fix the import
// promise never settled and the loading state spun forever.
const _VIS_CDNS = [
  // Local vendor copy — tried first so offline use works. IMPORTANT:
  // this MUST be the *standalone* build (bundles vis-data → exposes
  // DataSet). The plain peer-dist `vis-network.min.js` only has
  // Network, so `new vis.DataSet()` throws "vis.DataSet is not a
  // constructor". _resolveVis() below now validates Network+DataSet
  // and skips any build missing DataSet, so a wrong vendor file no
  // longer hard-fails the graph — it just falls through to a CDN.
  "/ui/vendor/vis-network.min.js",
  "https://esm.sh/vis-network@9.1.9/standalone",
  "https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/esm/vis-network.min.js",
  "https://unpkg.com/vis-network@9.1.9/standalone/esm/vis-network.min.js",
];

// A loaded vis module can surface its API in several shapes depending on
// build/format: named ESM exports (mod.Network), a default wrapper
// (mod.default.Network), or a UMD global side-effect (window.vis). Return
// the object that has BOTH Network and DataSet, or null if none does.
function _resolveVis(mod) {
  const candidates = [mod, mod && mod.default, (typeof window !== "undefined" && window.vis) || null];
  for (const c of candidates) {
    if (c && typeof c.Network === "function" && typeof c.DataSet === "function") {
      return c;
    }
  }
  return null;
}
const _VIS_CDN_TIMEOUT_MS = 12000;  // per-CDN cap

function _withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms),
    ),
  ]);
}

async function _loadVisNetwork() {
  if (_visModule) return _visModule;
  if (_visPromise) return _visPromise;
  _visPromise = (async () => {
    const errors = [];
    for (const url of _VIS_CDNS) {
      try {
        const mod = await _withTimeout(
          import(url),
          _VIS_CDN_TIMEOUT_MS,
          `vis-network@${url}`,
        );
        const vis = _resolveVis(mod);
        if (!vis) {
          // Module loaded but lacks DataSet (e.g. peer-dist build, not
          // standalone). Don't accept it — fall through to a CDN that
          // bundles vis-data.
          errors.push(`${url}: 缺少 DataSet（非 standalone 构建）`);
          continue;
        }
        _visModule = vis;
        return vis;
      } catch (err) {
        errors.push(`${url}: ${err && err.message || err}`);
        continue;
      }
    }
    _visPromise = null;
    throw new Error(
      "vis-network 无法加载（所有源均失败）。已尝试:\n  - "
      + errors.join("\n  - ")
      + "\n\n离线修复：把 vis-network 的 *standalone* 构建"
      + "（standalone/esm/vis-network.min.js，含 DataSet）放到 "
      + "xmclaw/daemon/static/vendor/vis-network.min.js 后刷新。"
      + "注意必须是 standalone 版，普通 dist 版不含 DataSet。",
    );
  })();
  return _visPromise;
}


// ── Truncate helper ──────────────────────────────────────────────


// Wave-32+ UX fix: graph-panel action button with inline progress.
// Pre-fix the LLM-触发 buttons just sat there silently for 10-30s
// while the call was in flight — no spinner, no "disabled" cue,
// nothing. User saw a static button + then suddenly an alert; if
// the call timed out at the network layer they got "Failed to
// fetch" with no warning the work was even happening.
//
// Wave-32+ (2026-05-19) follow-up: bypass the shared apiPost so we
// can attach an AbortController with a 70s ceiling. The server side
// fences these routes at 55s, but if the daemon hangs upstream of
// FastAPI (e.g. uvicorn event loop blocked by an unrelated heavy
// task) the browser would just sit on a stuck TCP socket until its
// own network-layer timeout — surfacing as a cryptic "Failed to
// fetch" with no diagnostic. Now we always surface a structured
// error: either the route's own JSON timeout payload (clean), or
// our client-side "请求超时" if the connection itself stalled.
const _LLM_TOPIC_FETCH_TIMEOUT_MS = 70000;

async function _postWithTimeout(path, body, token, timeoutMs) {
  const url = path + (token ? `?token=${encodeURIComponent(token)}` : "");
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      signal: ctrl.signal,
    });
    let json = null;
    try { json = await res.json(); } catch (_) { /* allow empty */ }
    if (!res.ok) {
      const detail = json && (json.detail || json.error);
      const err = new Error(
        `${res.status} ${res.statusText}${detail ? `: ${detail}` : ""}`,
      );
      err.status = res.status;
      throw err;
    }
    return json;
  } catch (e) {
    if (e && e.name === "AbortError") {
      const secs = Math.round(timeoutMs / 1000);
      const err = new Error(
        `请求超时（前端 ${secs}s 上限触发）。可能正在大库扫描，` +
        "稍后重试，或先用 budget=1 试探。",
      );
      err.timedOut = true;
      throw err;
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

function _GraphActionButton({
  label, runningLabel, title, token, path, body, onDone, formatResult,
}) {
  const [busy, setBusy] = useState(false);
  return h(
    "button",
    {
      type: "button",
      class: "xmc-h-btn",
      title,
      disabled: busy,
      style: "font-size:.78rem;padding:.3rem .6rem;"
        + (busy ? "opacity:.65;cursor:wait" : ""),
      onClick: async () => {
        if (busy) return;
        setBusy(true);
        try {
          const r = await _postWithTimeout(
            path, body || {}, token, _LLM_TOPIC_FETCH_TIMEOUT_MS,
          );
          // The route wraps internal errors as 200 + {ok:false,error}
          // so a network-level failure here is genuinely an
          // infrastructure problem worth surfacing.
          const d = (r && r.ok && (r.data || r)) || r || {};
          alert(formatResult(d));
          if (typeof onDone === "function") onDone();
        } catch (e) {
          alert(label + " 失败: " + (e && e.message || e));
        } finally {
          setBusy(false);
        }
      },
    },
    busy ? runningLabel : label,
  );
}


// Wave-32+ graph position cache. Keyed per node, bounded to keep
// localStorage well under quota (typical browser cap ≈ 5MB, this
// ceiling at 1000 nodes × ~30 bytes/entry stays under 30KB).
const _POSITIONS_KEY = "xmc.mem.graph.positions";
const _POSITIONS_MAX = 1000;
const _POSITIONS_VERSION = 1;

function _loadGraphPositions() {
  try {
    const raw = localStorage.getItem(_POSITIONS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    // Version gate — if the schema changes we drop stale entries
    // rather than rendering them at incompatible coordinates.
    if (parsed.v !== _POSITIONS_VERSION) return {};
    return parsed.p || {};
  } catch (_) {
    return {};
  }
}

function _saveGraphPositions(positions, token) {
  if (!positions || typeof positions !== "object") return;
  // Merge with the existing saved set so nodes not currently in the
  // viewport (e.g. focus-filtered) retain their saved positions.
  const existing = _loadGraphPositions();
  const merged = { ...existing };
  let count = Object.keys(merged).length;
  for (const [id, pos] of Object.entries(positions)) {
    if (!pos || typeof pos.x !== "number" || typeof pos.y !== "number") continue;
    if (!(id in merged) && count >= _POSITIONS_MAX) continue;
    merged[id] = { x: Math.round(pos.x), y: Math.round(pos.y) };
    count = Object.keys(merged).length;
  }
  try {
    localStorage.setItem(_POSITIONS_KEY, JSON.stringify({
      v: _POSITIONS_VERSION,
      p: merged,
    }));
  } catch (_) {
    // QuotaExceededError — try once with just the new positions
    // (drop the merged history) before giving up.
    try {
      localStorage.setItem(_POSITIONS_KEY, JSON.stringify({
        v: _POSITIONS_VERSION,
        p: positions,
      }));
    } catch (__) { /* give up */ }
  }
  // Wave-32+ Chunk 8: also push to the server so positions sync
  // across browsers / devices. Debounced via the caller's snapshot
  // events (stabilization-done + drag-end, both relatively rare).
  // Fire-and-forget — failure here doesn't affect the local UI;
  // localStorage retains the data.
  if (token) {
    _pushPositionsToServer(merged, token);
  }
}


// Wave-32+ Chunk 8: debounced server-side push so we don't spam
// PUT requests on every micro-stabilization step. 1.5s window is
// generous — the user typically finishes dragging well within it.
let _serverPushTimer = null;
let _serverPushPending = null;

function _pushPositionsToServer(positions, token) {
  _serverPushPending = positions;
  if (_serverPushTimer) return;
  _serverPushTimer = setTimeout(async () => {
    const pendingPos = _serverPushPending;
    _serverPushPending = null;
    _serverPushTimer = null;
    if (!pendingPos) return;
    try {
      const url = "/api/v2/memory/v2/graph_positions"
        + (token ? `?token=${encodeURIComponent(token)}` : "");
      await fetch(url, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ positions: pendingPos }),
      });
    } catch (_) { /* server unreachable / not yet on new daemon */ }
  }, 1500);
}


async function _fetchPositionsFromServer(token) {
  if (!token) return null;
  try {
    const url = "/api/v2/memory/v2/graph_positions"
      + `?token=${encodeURIComponent(token)}`;
    const resp = await fetch(url);
    if (!resp.ok) return null;
    const body = await resp.json();
    if (!body || body.ok === false) return null;
    const positions = body.positions;
    if (!positions || typeof positions !== "object") return null;
    return positions;
  } catch (_) {
    return null;
  }
}


// Wave-32+ alpha-blended hex color for edge strength encoding.
// Accepts "#RRGGBB" or "rgb(...)" — returns "rgba(r, g, b, a)".
// Pre-fix: edges had constant color regardless of confidence. A 0.6
// token-bridge looked exactly the same as a 1.0 user-confirmed
// edge. Now color encodes confidence so the user can SEE which
// links are load-bearing in the cluster.
function _withAlpha(color, alpha) {
  if (!color) return `rgba(160,160,160,${alpha})`;
  const a = Math.max(0, Math.min(1, alpha));
  // Hex: #RRGGBB or #RGB.
  if (color.startsWith("#")) {
    let r, g, b;
    if (color.length === 7) {
      r = parseInt(color.slice(1, 3), 16);
      g = parseInt(color.slice(3, 5), 16);
      b = parseInt(color.slice(5, 7), 16);
    } else if (color.length === 4) {
      r = parseInt(color[1] + color[1], 16);
      g = parseInt(color[2] + color[2], 16);
      b = parseInt(color[3] + color[3], 16);
    } else {
      return color;
    }
    return `rgba(${r},${g},${b},${a})`;
  }
  // rgb(r, g, b) → rgba(r, g, b, a).
  const m = color.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
  if (m) return `rgba(${m[1]},${m[2]},${m[3]},${a})`;
  return color;
}


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

      // Wave-32+ position persistence: restore each node's saved
      // (x,y). Two-tier read: server-side first (syncs across
      // browsers / devices), localStorage as fallback (works
      // offline / before user touched the new endpoint). The
      // server-side fetch is awaited so first paint already has
      // restored positions; localStorage stays the auth-free
      // immediate cache.
      const serverPositions = await _fetchPositionsFromServer(token);
      const savedPositions = serverPositions || _loadGraphPositions();
      // If server returned non-empty data, mirror into localStorage
      // so subsequent renders don't re-fetch unnecessarily.
      if (serverPositions && Object.keys(serverPositions).length > 0) {
        try {
          localStorage.setItem(_POSITIONS_KEY, JSON.stringify({
            v: _POSITIONS_VERSION,
            p: serverPositions,
          }));
        } catch (_) { /* quota */ }
      }
      // Map nodes → vis-network shape.
      const visNodes = data.nodes.map((n) => {
        const color = KIND_COLOR[n.kind] || "#777";
        const evidence = n.evidence_count || 1;
        const size = Math.min(40, 12 + evidence * 4);
        const isEvent = n.kind === "event";
        const node = {
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
        const saved = savedPositions[n.id];
        if (saved && typeof saved.x === "number" && typeof saved.y === "number") {
          node.x = saved.x;
          node.y = saved.y;
          // ``fixed`` would freeze them entirely; we want them
          // restored but still movable by the user.
        }
        return node;
      });

      const visEdges = data.edges.map((e) => {
        const style = RELATION_COLOR[e.relation] || { color: "#999", width: 1 };
        // Wave-32+ weighted edge rendering. Backend has populated
        // relation.strength all along (token-bridge=0.6,
        // entity-share=0.85, LLM-judged=0.85, co-extracted-from-
        // same-message=0.80, user-confirmed=1.0), but the UI was
        // rendering every edge at the kind's static width — so all
        // SAME_TOPIC edges looked identical regardless of how
        // confident the system was. Now line thickness encodes
        // strength: width = baseWidth * clamp(0.5 + strength, 0.5, 2.5).
        // Strong edges (entity-share, LLM-confirmed) stand out;
        // weak (token-only) recede into the background visually.
        const strength = typeof e.strength === "number" ? e.strength : 0.5;
        const weightMultiplier = Math.max(0.5, Math.min(2.5, 0.5 + strength));
        // Opacity also tracks strength so weak edges literally fade
        // — for a dense graph the user can SEE which links are
        // load-bearing.
        const alpha = Math.max(0.35, Math.min(1.0, 0.4 + strength * 0.6));
        return {
          id: e.id,
          from: e.source,
          to: e.target,
          label: e.relation,
          title: `${e.relation} (strength ${strength.toFixed(2)})`,
          color: {
            color: _withAlpha(style.color, alpha),
            highlight: "#fff",
          },
          width: style.width * weightMultiplier,
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
        // Wave-32+ position persistence: snapshot positions when
        // the layout stabilizes (initial force-directed converge)
        // AND when the user finishes dragging a node. Use a single
        // helper so both paths agree on shape + storage key.
        const _snapshotPositions = () => {
          try {
            const positions = network.getPositions();
            _saveGraphPositions(positions, token);
          } catch (_) { /* localStorage full / disabled */ }
        };
        network.on("stabilizationIterationsDone", _snapshotPositions);
        network.on("dragEnd", (params) => {
          if (params.nodes && params.nodes.length > 0) {
            _snapshotPositions();
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
        <${_GraphActionButton}
          label="🧱 实体索引"
          runningLabel="🧱 重建中..."
          title="扫一遍所有 fact，重建实体反向索引。升级后第一次必跑（已有数据没在索引里）。"
          token=${token}
          path="/api/v2/memory/v2/entity_index_rebuild"
          body=${{}}
          onDone=${refresh}
          formatResult=${(d) =>
            "实体索引重建完成\n扫描事实: " + (d.scanned || 0) +
            "\n注册成功: " + (d.registered || 0) +
            "\n错误: " + (d.errors || 0) +
            "\n已保存到磁盘: " + (d.saved ? "是" : "否")
          }
        />
        <${_GraphActionButton}
          label="🔗 重建关系"
          runningLabel="🔗 重建中..."
          title="按 Wave-32+ 新规则（跨 kind + 共享实体桥接）扫一遍所有事实，补上漏链的 SAME_TOPIC 边"
          token=${token}
          path="/api/v2/memory/v2/relink_same_topic"
          body=${{}}
          onDone=${refresh}
          formatResult=${(d) =>
            "重建关系完成\n扫描: " + (d.scanned || 0) +
            "\n新增边: " + (d.edges_added || 0) +
            "\n跳过(已存在): " + (d.edges_skipped || 0)
          }
        />
        <${_GraphActionButton}
          label="🧠 LLM 细化"
          runningLabel="🧠 调 LLM 中..."
          title="对向量相似但未达阈值的边缘对，让 LLM 判断是否真的是同一主题（每次最多 20 对，1 次 LLM 调用）"
          token=${token}
          path="/api/v2/memory/v2/llm_topic_refine"
          body=${{ budget: 20 }}
          onDone=${refresh}
          formatResult=${(d) => {
            if (d.error) return "LLM 细化失败: " + d.error;
            return "LLM 关系细化完成\n判断对数: " + (d.scanned_pairs || 0) +
              "\n新增边: " + (d.edges_added || 0) +
              "\nLLM 调用: " + (d.llm_calls || 0) +
              "\n耗时: " + (d.duration_s || 0) + "s";
          }}
        />
        <${_GraphActionButton}
          label="🏷️ 起主题名"
          runningLabel="🏷️ 起名中..."
          title="对 SAME_TOPIC 簇（≥3 事实，未命名）让 LLM 起 2-8 字主题标题（每簇 1 次 LLM 调用，每次最多处理 5 簇）"
          token=${token}
          path="/api/v2/memory/v2/llm_topic_name"
          body=${{ budget: 5 }}
          onDone=${refresh}
          formatResult=${(d) => {
            if (d.error) return "LLM 命名失败: " + d.error;
            return "LLM 主题命名完成\n候选簇: " + (d.clusters_scanned || 0) +
              "\n新主题节点: " + (d.topics_created || 0) +
              "\n跳过(已有名): " + (d.clusters_skipped_already_named || 0) +
              "\nLLM 调用: " + (d.llm_calls || 0) +
              "\n耗时: " + (d.duration_s || 0) + "s";
          }}
        />
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
