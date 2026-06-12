// 记忆图谱（2026-06-12 用户点名加回）。数据源 /api/v2/memory/v2/graph
// （nodes + relation edges）。自绘 SVG 力导向布局 — 节点量级 ≤100，
// 不值得为此引入 1MB 的 vis-network；保存过的手动布局
// （/graph_positions）优先生效。点节点 → 高亮邻边 + 底部详情。

import { useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../store/app";
import { apiGet } from "../lib/api";

interface GNode {
  id: string;
  kind: string;
  scope: string;
  text: string;
  confidence?: number;
}
interface GEdge {
  id: string;
  source: string;
  target: string;
  relation: string;
  strength?: number;
}

const KIND_COLOR: Record<string, string> = {
  preference: "#8b5cf6",
  decision: "#67e8f9",
  identity: "#f472b6",
  commitment: "#fbbf24",
  correction: "#f87171",
  project: "#93c5fd",
  episode: "#34d399",
  lesson: "#a3e635",
  fact: "#94a3b8",
  event: "#64748b",
};

const W = 900;
const H = 520;

// 简易力导向：库仑斥力 + 边弹簧 + 中心引力，固定迭代次数一次算完。
function layout(nodes: GNode[], edges: GEdge[], pinned: Record<string, { x: number; y: number }>) {
  const pos: Record<string, { x: number; y: number }> = {};
  nodes.forEach((n, i) => {
    if (pinned[n.id]) {
      pos[n.id] = { ...pinned[n.id] };
      return;
    }
    const angle = (i / Math.max(1, nodes.length)) * Math.PI * 2;
    const r = 150 + (i % 5) * 28;
    pos[n.id] = { x: W / 2 + Math.cos(angle) * r, y: H / 2 + Math.sin(angle) * r };
  });
  const idset = new Set(nodes.map((n) => n.id));
  const links = edges.filter((e) => idset.has(e.source) && idset.has(e.target));
  for (let iter = 0; iter < 120; iter++) {
    const t = 1 - iter / 120;
    const disp: Record<string, { x: number; y: number }> = {};
    nodes.forEach((n) => (disp[n.id] = { x: 0, y: 0 }));
    // 斥力
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = pos[nodes[i].id];
        const b = pos[nodes[j].id];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        const d2 = Math.max(64, dx * dx + dy * dy);
        const f = 2600 / d2;
        const d = Math.sqrt(d2);
        dx /= d;
        dy /= d;
        disp[nodes[i].id].x += dx * f;
        disp[nodes[i].id].y += dy * f;
        disp[nodes[j].id].x -= dx * f;
        disp[nodes[j].id].y -= dy * f;
      }
    }
    // 弹簧
    for (const e of links) {
      const a = pos[e.source];
      const b = pos[e.target];
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.max(8, Math.sqrt(dx * dx + dy * dy));
      const f = (d - 90) * 0.04;
      disp[e.source].x += (dx / d) * f;
      disp[e.source].y += (dy / d) * f;
      disp[e.target].x -= (dx / d) * f;
      disp[e.target].y -= (dy / d) * f;
    }
    // 中心引力 + 应用位移
    nodes.forEach((n) => {
      if (pinned[n.id]) return;
      const p = pos[n.id];
      p.x += (disp[n.id].x + (W / 2 - p.x) * 0.015) * t;
      p.y += (disp[n.id].y + (H / 2 - p.y) * 0.015) * t;
      p.x = Math.min(W - 20, Math.max(20, p.x));
      p.y = Math.min(H - 20, Math.max(20, p.y));
    });
  }
  return { pos, links };
}

export default function MemoryGraph() {
  const token = useApp((s) => s.token);
  const [nodes, setNodes] = useState<GNode[]>([]);
  const [edges, setEdges] = useState<GEdge[]>([]);
  const [pinned, setPinned] = useState<Record<string, { x: number; y: number }>>({});
  const [sel, setSel] = useState<string | null>(null);
  const [err, setErr] = useState("");
  const svgRef = useRef<SVGSVGElement>(null);
  // 视口：滚轮缩放（围绕鼠标点）+ 空白处拖拽平移。
  const [view, setView] = useState({ x: 0, y: 0, w: W, h: H });
  const viewRef = useRef(view);
  viewRef.current = view;
  const panRef = useRef<{ startX: number; startY: number; vx: number; vy: number } | null>(null);

  // React 的 onWheel 是 passive（preventDefault 无效会连页面一起滚），
  // 原生监听绕开。
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const v = viewRef.current;
      const rect = svg.getBoundingClientRect();
      const px = v.x + ((e.clientX - rect.left) / rect.width) * v.w;
      const py = v.y + ((e.clientY - rect.top) / rect.height) * v.h;
      const k = Math.pow(1.0015, -e.deltaY);
      const w = Math.min(W * 1.5, Math.max(W / 10, v.w / k));
      const scale = w / v.w;
      setView({
        x: px - (px - v.x) * scale,
        y: py - (py - v.y) * scale,
        w,
        h: w * (H / W),
      });
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
    // 依赖 nodes.length：首渲染走"加载中"早退分支时 svgRef 还是 null，
    // 空依赖会让监听器永远挂不上（实测踩过）。
  }, [nodes.length > 0]);

  const zoom = W / view.w;

  useEffect(() => {
    if (!token) return;
    Promise.all([
      apiGet<{ nodes?: GNode[]; edges?: GEdge[] }>("/api/v2/memory/v2/graph?limit=80", token),
      apiGet<{ positions?: Record<string, { x: number; y: number }> }>(
        "/api/v2/memory/v2/graph_positions",
        token,
      ).catch(() => ({ positions: {} })),
    ])
      .then(([g, p]) => {
        setNodes(g?.nodes || []);
        setEdges(g?.edges || []);
        setPinned(p?.positions || {});
      })
      .catch((e) => setErr(String(e?.message || e)));
  }, [token]);

  const { pos, links } = useMemo(() => layout(nodes, edges, pinned), [nodes, edges, pinned]);

  const selNode = nodes.find((n) => n.id === sel);
  const selEdges = useMemo(
    () => (sel ? links.filter((e) => e.source === sel || e.target === sel) : []),
    [sel, links],
  );
  const neighborIds = useMemo(
    () => new Set(selEdges.flatMap((e) => [e.source, e.target])),
    [selEdges],
  );

  // 节点拖拽（拖完位置留在本地 pinned；不强制写回 server，保持只读首版）。
  const dragId = useRef<string | null>(null);
  function svgPoint(ev: React.MouseEvent): { x: number; y: number } {
    const svg = svgRef.current!;
    const rect = svg.getBoundingClientRect();
    const v = viewRef.current;
    return {
      x: v.x + ((ev.clientX - rect.left) / rect.width) * v.w,
      y: v.y + ((ev.clientY - rect.top) / rect.height) * v.h,
    };
  }

  if (err) return <div className="text-xs text-mc-err p-3">图谱加载失败：{err}</div>;
  if (nodes.length === 0)
    return <div className="text-xs text-mc-faint p-3">暂无图谱数据 — 记忆之间产生关联后出现</div>;

  return (
    <div>
      <svg
        ref={svgRef}
        viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`}
        className="w-full rounded-lg border border-mc-border bg-black/20 select-none"
        style={{ cursor: panRef.current ? "grabbing" : "grab", touchAction: "none" }}
        onMouseDown={(ev) => {
          // 空白处按下 = 平移（节点的 onMouseDown 会先设 dragId 并阻断冒泡）。
          if (dragId.current) return;
          panRef.current = { startX: ev.clientX, startY: ev.clientY, vx: view.x, vy: view.y };
        }}
        onMouseMove={(ev) => {
          if (dragId.current) {
            const p = svgPoint(ev);
            setPinned((cur) => ({ ...cur, [dragId.current!]: p }));
            return;
          }
          const pan = panRef.current;
          if (pan) {
            const rect = svgRef.current!.getBoundingClientRect();
            const v = viewRef.current;
            setView({
              ...v,
              x: pan.vx - ((ev.clientX - pan.startX) / rect.width) * v.w,
              y: pan.vy - ((ev.clientY - pan.startY) / rect.height) * v.h,
            });
          }
        }}
        onMouseUp={() => {
          dragId.current = null;
          panRef.current = null;
        }}
        onMouseLeave={() => {
          dragId.current = null;
          panRef.current = null;
        }}
        onDoubleClick={() => setView({ x: 0, y: 0, w: W, h: H })}
      >
        {links.map((e) => {
          const a = pos[e.source];
          const b = pos[e.target];
          if (!a || !b) return null;
          const hot = sel && (e.source === sel || e.target === sel);
          return (
            <line
              key={e.id}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke={hot ? "#8b5cf6" : "#2a3245"}
              strokeWidth={hot ? 1.6 : 0.8}
              opacity={sel && !hot ? 0.25 : 1}
            />
          );
        })}
        {nodes.map((n) => {
          const p = pos[n.id];
          if (!p) return null;
          const c = KIND_COLOR[n.kind] || "#94a3b8";
          const dim = sel && n.id !== sel && !neighborIds.has(n.id);
          const r = 5 + Math.min(4, (n.confidence || 0.5) * 4);
          return (
            <g
              key={n.id}
              transform={`translate(${p.x},${p.y})`}
              opacity={dim ? 0.25 : 1}
              className="cursor-pointer"
              onMouseDown={(ev) => {
                ev.stopPropagation();
                dragId.current = n.id;
              }}
              onClick={() => setSel(sel === n.id ? null : n.id)}
            >
              <circle r={r} fill={c} stroke={n.id === sel ? "#fff" : "transparent"} strokeWidth={1.5} />
              {/* 放大后无需点击即显示标签（zoom≥1.5 全亮；2.5 倍后显示更长文本）。 */}
              {(n.id === sel || zoom >= 1.5 || nodes.length <= 30) && (
                <text y={-r - 4} textAnchor="middle" fontSize={9} fill="#bdc8da">
                  {n.text.slice(0, zoom >= 2.5 ? 40 : 14)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="flex gap-2 flex-wrap mt-2">
        {Object.entries(KIND_COLOR)
          .filter(([k]) => nodes.some((n) => n.kind === k))
          .map(([k, c]) => (
            <span key={k} className="flex items-center gap-1 text-[10.5px] text-mc-faint">
              <span className="w-2 h-2 rounded-full" style={{ background: c }} />
              {k}
            </span>
          ))}
        <span className="text-[10.5px] text-mc-faint ml-auto">
          {nodes.length} 节点 · {links.length} 边 · 滚轮缩放{zoom > 1.05 ? ` ${zoom.toFixed(1)}×` : ""} · 拖空白平移 · 双击复位
        </span>
      </div>
      {selNode && (
        <div className="mt-2 border border-mc-border rounded-md px-3 py-2 bg-mc-panel2/40">
          <div className="text-[13px] leading-relaxed">{selNode.text}</div>
          <div className="flex gap-2 mt-1 text-[10.5px] text-mc-faint">
            <span className="text-mc-accent">{selNode.kind}</span>
            <span>{selNode.scope}</span>
            <span>{selEdges.length} 条关联</span>
          </div>
          {selEdges.slice(0, 6).map((e) => {
            const otherId = e.source === selNode.id ? e.target : e.source;
            const other = nodes.find((n) => n.id === otherId);
            return (
              <button
                key={e.id}
                onClick={() => setSel(otherId)}
                className="block text-left text-[11px] text-mc-muted hover:text-mc-accent cursor-pointer mt-1 truncate max-w-full"
              >
                ↔ {e.relation} · {(other?.text || otherId).slice(0, 50)}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
