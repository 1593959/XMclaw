// XMclaw — CognitionPage (Jarvisification Phase 3).
//
// Surfaces the live cognitive architecture state:
//   - Attention focus (what the system is paying attention to)
//   - Current goals
//   - Task queue from TaskScheduler
//   - Evolution proposals pending review
//   - MemoryGraph statistics

const { h } = window.__xmc.preact;
const { useEffect, useState, useRef } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { useSafePost } from "../lib/use_safe_fetch.js";
import { toast } from "../lib/toast.js";
// R6 (2026-05-10) — 心智可视化新增的两个 panel:
//   * 内心独白 — 显示 ReflectionCycle (R1) 产生的 INNER_MONOLOGUE 事件
//   * 建议盒子 — 显示 SuggestionInbox (R5) 待审 / 已审建议
import { InnerMonologuePanel } from "./_panels/mind_inner_monologue.js";
import { SuggestionsPanel } from "./_panels/mind_suggestions.js";

// Lazy-load Mermaid for DAG visualisation.
let _mermaidPromise = null;
function loadMermaid() {
  if (!_mermaidPromise) {
    _mermaidPromise = import("https://esm.sh/mermaid@10/dist/mermaid.esm.min.mjs").then((m) => {
      m.default.initialize({ startOnLoad: false, theme: "dark" });
      return m.default;
    });
  }
  return _mermaidPromise;
}

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function Card({ title, children }) {
  return html`
    <div style="border:1px solid var(--color-border);border-radius:8px;padding:16px;margin-bottom:16px;background:var(--color-surface)">
      <h3 style="margin:0 0 12px;font-size:1rem;font-weight:600">${title}</h3>
      ${children}
    </div>
  `;
}

function Badge({ text, tone = "neutral" }) {
  const colors = {
    neutral: "#888",
    success: "#2ecc71",
    warning: "#f39c12",
    danger: "#e74c3c",
    info: "#3498db",
  };
  return html`
    <span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:.75rem;background:${colors[tone] || colors.neutral}22;color:${colors[tone] || colors.neutral};border:1px solid ${colors[tone] || colors.neutral}44">
      ${text}
    </span>
  `;
}

export function CognitionPage({ token }) {
  const [state, setState] = useState("loading");
  const [cogState, setCogState] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [proposals, setProposals] = useState([]);
  const [graphStats, setGraphStats] = useState(null);
  const [taskGraph, setTaskGraph] = useState(null);
  const [daemonHealth, setDaemonHealth] = useState(null);
  const [error, setError] = useState(null);
  const dagRef = useRef(null);
  // R6: which top-level "心智" tab is active. Default = state
  // (the legacy live-state grid). New tabs: 内心独白 + 建议盒子.
  const [tab, setTab] = useState("state");
  const isMountedRef = useRef(true);
  const { run: postFn } = useSafePost(token);

  async function loadAll() {
    try {
      const [s, t, p, g, tg, dh] = await Promise.all([
        apiGet("/api/v2/cognition/state", token),
        apiGet("/api/v2/cognition/tasks", token),
        apiGet("/api/v2/cognition/proposals", token),
        apiGet("/api/v2/cognition/graph/stats", token),
        apiGet("/api/v2/cognition/tasks/graph", token),
        apiGet("/api/v2/cognition/daemon/health", token).catch(() => null),
      ]);
      if (!isMountedRef.current) return;
      setCogState(s);
      setTasks(t.tasks || []);
      setProposals(p.proposals || []);
      setGraphStats(g);
      setTaskGraph(tg);
      setDaemonHealth(dh && dh.ok ? dh : null);
      setState("ready");
    } catch (e) {
      if (!isMountedRef.current) return;
      setError(e);
      setState("error");
    }
  }

  useEffect(() => {
    isMountedRef.current = true;
    loadAll();
    const iv = setInterval(loadAll, 5000);

    // Phase 5: real-time cognitive state via WebSocket.
    // Falls back gracefully to the 5s polling if WS disconnects.
    let ws = null;
    try {
      const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const wsUrl = new URL(wsProto + "//" + window.location.host + "/api/v2/cognition/ws");
      if (token) wsUrl.searchParams.set("token", token);
      ws = new WebSocket(wsUrl.toString());
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data);
          if (!data.error) {
            setCogState(data);
          }
        } catch (_) {
          /* ignore malformed frames */
        }
      };
      ws.onopen = () => {
        // WS connected — cognitive state will arrive in real-time.
        // The 5s polling still refreshes tasks / proposals / graph.
      };
    } catch (_) {
      ws = null;
    }

    return () => {
      isMountedRef.current = false;
      clearInterval(iv);
      if (ws) {
        try { ws.close(); } catch (_) {}
      }
    };
  }, [token]);

  async function onApprove(id) {
    const r = await postFn("POST", `/api/v2/cognition/proposals/${id}/approve`, {});
    if (r.ok) {
      loadAll();
    } else {
      toast.error(`approve 失败: ${r.error.message || r.error}`);
    }
  }

  async function onReject(id) {
    const r = await postFn("POST", `/api/v2/cognition/proposals/${id}/reject`, {});
    if (r.ok) {
      loadAll();
    } else {
      toast.error(`reject 失败: ${r.error.message || r.error}`);
    }
  }

  if (state === "loading") {
    return html`<div style="padding:40px;text-align:center">加载认知状态…</div>`;
  }
  if (state === "error") {
    // 503 with structured "reason" field (from routers/cognition.py
    // _not_wired) → render the actionable "how to enable" panel.
    // Other errors still get the bare retry box.
    const body = error && error.body;
    const isCognitionOff = error && error.status === 503 && body && body.reason;
    if (isCognitionOff) {
      const reason = body.reason;
      const title = reason === "failed_startup"
        ? "🛠️ 认知子系统启动失败"
        : "🌒 认知模块未启用";
      return html`
        <div style="padding:32px;max-width:720px;margin:0 auto">
          <div style="font-size:1.4rem;font-weight:600;margin-bottom:8px">
            ${title}
          </div>
          <div style="opacity:.75;font-size:.95rem;line-height:1.55;margin-bottom:20px">
            ${body.hint || "Cognition not wired."}
          </div>
          <div style="background:var(--xmc-bg-soft, rgba(255,255,255,.03));
                      border:1px solid var(--xmc-border, rgba(255,255,255,.1));
                      border-radius:8px;padding:18px 20px">
            <div style="font-weight:600;margin-bottom:10px">
              如何打开
            </div>
            <ol style="margin:0;padding-left:20px;line-height:1.7;font-size:.92rem">
              ${(body.how_to_enable || []).map((step) => html`<li>${step}</li>`)}
            </ol>
          </div>
          <div style="margin-top:18px;display:flex;gap:8px">
            <button onClick=${loadAll}
                    style="padding:8px 16px;background:var(--xmc-accent);
                           color:#000;border:none;border-radius:6px;
                           cursor:pointer;font-weight:500">
              重试加载
            </button>
            <a href="/ui/settings" style="padding:8px 16px;
                                          border:1px solid var(--xmc-border);
                                          border-radius:6px;
                                          text-decoration:none;
                                          color:var(--xmc-fg)">
              打开 Settings
            </a>
          </div>
          <details style="margin-top:18px;font-size:.82rem;opacity:.6">
            <summary style="cursor:pointer">原始错误（debug）</summary>
            <pre style="margin-top:8px;white-space:pre-wrap;
                        font-family:var(--xmc-font-mono, monospace);
                        font-size:.78rem">${String(error)}</pre>
          </details>
        </div>
      `;
    }
    return html`
      <div style="padding:40px;text-align:center">
        <div style="color:var(--xmc-danger)">加载失败</div>
        <div style="font-size:.85rem;opacity:.7;margin-top:8px">${String(error)}</div>
        <button onClick=${loadAll} style="margin-top:16px">重试</button>
      </div>
    `;
  }

  // R6: tab nav for the three-pane "心智" view.
  const tabs = [
    { id: "state", label: "实时状态", hint: "注意力焦点 / 目标 / 任务 / 提案 / 图谱" },
    { id: "monologue", label: "内心独白", hint: "Agent 的反思 / 计划 / 担忧 (R1 ReflectionCycle)" },
    { id: "suggestions", label: "建议盒子", hint: "AutonomyPolicy surface 给你审批的主动建议 (R5)" },
  ];
  const activeMeta = tabs.find((t) => t.id === tab);

  // Non-state tabs render their own panel; state tab keeps the
  // legacy grid + WS-pushed live data.
  if (tab !== "state") {
    return html`
      <section class="xmc-datapage" aria-labelledby="cognition-title">
        <header class="xmc-datapage__header">
          <h2 id="cognition-title">🧠 认知状态</h2>
          <p class="xmc-datapage__subtitle">${activeMeta?.hint || ""}</p>
        </header>
        <nav class="xmc-mem-tabs" role="tablist" style="display:flex;gap:.4rem;border-bottom:1px solid var(--color-border);margin-bottom:.8rem;flex-wrap:wrap">
          ${tabs.map((t) => {
            const isActive = t.id === tab;
            return html`
              <button type="button" role="tab" aria-selected=${isActive} key=${t.id} onClick=${() => setTab(t.id)}
                style=${`appearance:none;background:none;border:none;padding:.5rem .9rem;font:inherit;cursor:pointer;color:${isActive ? "var(--color-primary)" : "var(--xmc-fg-muted)"};border-bottom:2px solid ${isActive ? "var(--color-primary)" : "transparent"};font-weight:${isActive ? "600" : "500"}`}>
                ${t.label}
              </button>
            `;
          })}
        </nav>
        ${tab === "monologue" ? html`<${InnerMonologuePanel} token=${token} />` : null}
        ${tab === "suggestions" ? html`<${SuggestionsPanel} token=${token} />` : null}
      </section>
    `;
  }

  return html`
    <section class="xmc-datapage" aria-labelledby="cognition-title">
      <header class="xmc-datapage__header">
        <h2 id="cognition-title">🧠 认知状态</h2>
        <p class="xmc-datapage__subtitle">
          注意力焦点 · 当前目标 · 任务队列 · 进化提案 · 记忆图谱（每 2 秒 WS 推送）
        </p>
      </header>
      <nav class="xmc-mem-tabs" role="tablist" style="display:flex;gap:.4rem;border-bottom:1px solid var(--color-border);margin-bottom:.8rem;flex-wrap:wrap">
        ${tabs.map((t) => {
          const isActive = t.id === tab;
          return html`
            <button type="button" role="tab" aria-selected=${isActive} key=${t.id} onClick=${() => setTab(t.id)}
              style=${`appearance:none;background:none;border:none;padding:.5rem .9rem;font:inherit;cursor:pointer;color:${isActive ? "var(--color-primary)" : "var(--xmc-fg-muted)"};border-bottom:2px solid ${isActive ? "var(--color-primary)" : "transparent"};font-weight:${isActive ? "600" : "500"}`}>
              ${t.label}
            </button>
          `;
        })}
      </nav>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:14px;align-items:start">

      <!-- Daemon Health (Phase E) -->
      <${Card} title="Cognitive Daemon">
        ${!daemonHealth
          ? html`<div style="opacity:.6;font-size:.9rem">daemon 未连接</div>`
          : html`
            <div style="display:flex;flex-direction:column;gap:10px">
              <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
                <${Badge} text=${daemonHealth.status.toUpperCase()} tone=${
                  daemonHealth.status === "healthy" ? "success" :
                  daemonHealth.status === "degraded" ? "warning" : "danger"
                } />
                <span style="font-size:.85rem;opacity:.8">
                  ticks: <strong>${daemonHealth.tick_count}</strong>
                </span>
                ${daemonHealth.memory_mb != null
                  ? html`<span style="font-size:.85rem;opacity:.8">mem: <strong>${daemonHealth.memory_mb} MB</strong></span>`
                  : null}
              </div>
              ${daemonHealth.last_tick
                ? html`
                  <div style="font-size:.8rem;opacity:.7;background:var(--color-background);border-radius:6px;padding:8px 12px">
                    <div>last tick #${daemonHealth.last_tick.tick}</div>
                    <div style="margin-top:4px">
                      ${Object.entries(daemonHealth.last_tick.latency_ms || {}).map(([k, v]) => html`
                        <span key=${k} style="display:inline-block;margin-right:10px">${k}: ${v}ms</span>
                      `)}
                    </div>
                    ${(daemonHealth.last_tick.errors || []).length
                      ? html`
                        <div style="margin-top:6px;color:var(--xmc-danger);font-size:.75rem">
                          ⚠ ${daemonHealth.last_tick.errors.join("; ")}
                        </div>
                      `
                      : null}
                  </div>
                `
                : null}
            </div>
          `}
      <//>

      <!-- Attention Focus -->
      <${Card} title="注意力焦点">
        ${!cogState?.attention_focus?.length
          ? html`<div style="opacity:.6;font-size:.9rem">暂无活跃焦点</div>`
          : html`
            <div style="display:flex;flex-direction:column;gap:8px">
              ${cogState.attention_focus.map((f) => html`
                <div key=${f.percept_id} style="display:flex;align-items:center;gap:12px;padding:8px 12px;background:var(--color-background);border-radius:6px">
                  <!-- 修复排版：flex 子项必须有 min-width:0
                       否则长路径（C:\...\.git\refs\...）撑爆 box，
                       右侧 salience badge 被推到下一行/中间。
                       word-break:break-all 让长路径在任意位置软断。 -->
                  <div style="flex:1;min-width:0;font-size:.9rem;word-break:break-all">${f.content}</div>
                  <${Badge} text=${`salience ${f.salience_score}`} tone=${f.salience_score > 0.7 ? "danger" : f.salience_score > 0.4 ? "warning" : "neutral"} />
                </div>
              `)}
            </div>
          `}
      <//>

      <!-- Goals -->
      <${Card} title="当前目标">
        ${!cogState?.goals?.length
          ? html`<div style="opacity:.6;font-size:.9rem">暂无目标</div>`
          : html`
            <div style="display:flex;flex-direction:column;gap:8px">
              ${cogState.goals.map((g) => html`
                <div key=${g.id} style="display:flex;align-items:center;gap:12px;padding:8px 12px;background:var(--color-background);border-radius:6px">
                  <div style="flex:1;min-width:0">
                    <div style="font-size:.9rem;word-break:break-word">${g.description}</div>
                    <div style="font-size:.75rem;opacity:.6">source: ${g.source}</div>
                  </div>
                  <${Badge} text=${`P${g.priority}`} tone=${g.priority >= 8 ? "danger" : g.priority >= 5 ? "warning" : "neutral"} />
                  <${Badge} text=${g.status} tone=${g.status === "active" ? "info" : "success"} />
                </div>
              `)}
            </div>
          `}
      <//>

      <!-- Tasks -->
      <${Card} title="任务队列">
        ${!tasks.length
          ? html`<div style="opacity:.6;font-size:.9rem">队列为空</div>`
          : html`
            <div style="display:flex;flex-direction:column;gap:8px">
              ${tasks.map((t) => html`
                <div key=${t.id} style="display:flex;align-items:center;gap:12px;padding:8px 12px;background:var(--color-background);border-radius:6px">
                  <div style="flex:1;min-width:0">
                    <div style="font-size:.85rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${t.prompt}</div>
                    <div style="font-size:.75rem;opacity:.6">retries: ${t.retries}/${t.max_retries}</div>
                  </div>
                  <${Badge} text=${t.status} tone=${
                    t.status === "completed" ? "success" :
                    t.status === "failed" || t.status === "escalated" ? "danger" :
                    t.status === "running" ? "info" : "neutral"
                  } />
                </div>
              `)}
            </div>
          `}
      <//>

      <!-- Proposals -->
      <${Card} title="进化提案">
        ${!proposals.length
          ? html`<div style="opacity:.6;font-size:.9rem">暂无待审提案</div>`
          : html`
            <div style="display:flex;flex-direction:column;gap:8px">
              ${proposals.map((p) => html`
                <div key=${p.id} style="display:flex;align-items:center;gap:12px;padding:8px 12px;background:var(--color-background);border-radius:6px">
                  <div style="flex:1;min-width:0">
                    <div style="font-size:.85rem;word-break:break-word">${p.description}</div>
                    <div style="font-size:.75rem;opacity:.6">target: ${p.target}</div>
                  </div>
                  <${Badge} text=${`conf ${p.confidence}`} tone=${p.confidence > 0.7 ? "success" : "warning"} />
                  <button onClick=${() => onApprove(p.id)} style="font-size:.75rem;padding:4px 8px">批准</button>
                  <button onClick=${() => onReject(p.id)} style="font-size:.75rem;padding:4px 8px">拒绝</button>
                </div>
              `)}
            </div>
          `}
      <//>

      <!-- Graph Stats -->
      <${Card} title="记忆图谱">
        ${!graphStats
          ? html`<div style="opacity:.6;font-size:.9rem">图谱未连接</div>`
          : html`
            <div style="display:flex;gap:24px;flex-wrap:wrap">
              <div>
                <div style="font-size:1.5rem;font-weight:700">${graphStats.nodes || 0}</div>
                <div style="font-size:.8rem;opacity:.6">节点</div>
              </div>
              <div>
                <div style="font-size:1.5rem;font-weight:700">${graphStats.edges || 0}</div>
                <div style="font-size:.8rem;opacity:.6">边</div>
              </div>
              ${Object.entries(graphStats.by_type || {}).map(([type, count]) => html`
                <div key=${type}>
                  <div style="font-size:1.5rem;font-weight:700">${count}</div>
                  <div style="font-size:.8rem;opacity:.6">${type}</div>
                </div>
              `)}
            </div>
          `}
      <//>

      </div>
    </section>
  `;
}


function TaskDag({ data }) {
  const ref = useRef(null);
  const [svg, setSvg] = useState(null);

  useEffect(() => {
    let cancelled = false;
    async function render() {
      const mermaid = await loadMermaid();
      if (cancelled) return;

      const { nodes, edges } = data;
      if (!nodes.length) return;

      // Build Mermaid graph definition.
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
