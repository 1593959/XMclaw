// 专家团视图（P0）：把 fanout / subagent 事件重组为结构化团队看板。
// 组长拆解 → 专家并行 → 整合。Mission Control 风格，深空墨底。

import { useEffect, useMemo, useState } from "react";
import { useApp } from "../store/app";
import { apiGetFresh } from "../lib/api";
import type { Entry } from "../lib/types";

const ROLE_META: Record<string, { icon: string; label: string; tone: string }> = {
  general:  { icon: "🧩", label: "通用",   tone: "text-mc-muted" },
  code:     { icon: "💻", label: "代码",   tone: "text-blue-400" },
  research: { icon: "🔬", label: "研究",   tone: "text-emerald-400" },
  ops:      { icon: "🛠", label: "运维",   tone: "text-amber-400" },
  comm:     { icon: "✍️", label: "文案",   tone: "text-pink-400" },
};

interface Round {
  id: string;
  leader: Entry | null;           // fanout 条目
  agents: Entry[];                // 该批次下的 subagent 条目
  ungrouped: boolean;             // 没有 fanout 头的散落 subagent
}

interface GraphNode {
  id?: string;
  task_id?: string;
  status?: string;
  dependencies?: string[];
  prompt?: string;
  description?: string;
}

interface GraphError {
  kind?: string;
  task_id?: string;
  node_id?: string;
  message?: string;
  error?: string;
}

interface GraphInspection {
  ok?: boolean;
  runnable?: string[];
  runnable_ids?: string[];
  blocked?: string[];
  blocked_ids?: string[];
  failed?: string[];
  failed_ids?: string[];
  cycles?: string[][];
}

interface GraphStateSnapshot {
  final?: string;
  confidence?: number;
  subtasks?: GraphNode[];
  errors?: GraphError[];
  metadata?: {
    inspection?: GraphInspection;
  };
}

function groupRounds(entries: Entry[]): Round[] {
  const rounds: Round[] = [];
  let current: Round | null = null;

  for (const e of entries) {
    if (e.kind === "fanout") {
      current = { id: e.id, leader: e, agents: [], ungrouped: false };
      rounds.push(current);
    } else if (e.kind === "subagent") {
      if (current) {
        current.agents.push(e);
      } else {
        // 散落 subagent → 未分组批次
        if (rounds.length === 0 || !rounds[rounds.length - 1].ungrouped) {
          rounds.push({ id: `ungrouped_${e.ts}`, leader: null, agents: [], ungrouped: true });
        }
        rounds[rounds.length - 1].agents.push(e);
      }
    }
  }

  return rounds;
}

function str(val: unknown): string {
  return typeof val === "string" ? val : "";
}

export default function TeamView() {
  const entries = useApp((s) => s.chat.entries);
  const token = useApp((s) => s.token);
  const rounds = useMemo(() => groupRounds(entries), [entries]);
  const [graphState, setGraphState] = useState<GraphStateSnapshot | null>(null);

  useEffect(() => {
    if (!token) {
      setGraphState(null);
      return;
    }

    const ctl = new AbortController();
    apiGetFresh<GraphStateSnapshot>(
      "/api/v2/cognition/tasks/graph-state",
      token,
      ctl.signal,
    )
      .then((snapshot) => setGraphState(snapshot || null))
      .catch(() => {
        if (!ctl.signal.aborted) {
          setGraphState(null);
        }
      });

    return () => ctl.abort();
  }, [token]);

  return (
    <div className="h-full flex flex-col min-w-0">
      {/* 顶栏 */}
      <div className="shrink-0 px-4 py-3 border-b border-mc-border flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-mc-text">👥 专家团</h2>
          <p className="text-xs text-mc-faint mt-0.5">组长拆解 · 并行执行 · 整合</p>
        </div>
      </div>

      {/* 内容 */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {graphState && <GraphStatePanel graphState={graphState} />}
        {rounds.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-mc-faint">
            <div className="text-4xl mb-3">👥</div>
            <p className="text-sm">还没有专家团在跑</p>
            <p className="text-xs mt-1 max-w-xs text-center">
              发个「并行对比 A/B/C」之类的任务，组长会拆给专家们并行做
            </p>
          </div>
        ) : (
          rounds.map((round) => (
            <RoundCard key={round.id} round={round} />
          ))
        )}
      </div>
    </div>
  );
}

function GraphStatePanel({ graphState }: { graphState: GraphStateSnapshot }) {
  const nodes = graphState.subtasks || [];
  const errors = graphState.errors || [];
  const inspection = graphState.metadata?.inspection || {};
  const runnable = inspection.runnable_ids || inspection.runnable || [];
  const blocked = inspection.blocked_ids || inspection.blocked || [];
  const failed = inspection.failed_ids || inspection.failed || [];
  const cycles = inspection.cycles || [];

  if (nodes.length === 0 && errors.length === 0 && !graphState.final) {
    return null;
  }

  return (
    <section className="border border-mc-border rounded-lg bg-mc-panel/50 overflow-hidden">
      <div className="px-4 py-3 border-b border-mc-border bg-mc-panel/70 flex flex-wrap items-center gap-2">
        <div className="mr-auto min-w-0">
          <h3 className="text-sm font-semibold text-mc-text">GraphState</h3>
          <p className="text-xs text-mc-faint mt-0.5">
            StateGraph task topology, reducers, blockers, and failure surface
          </p>
        </div>
        {graphState.final && <GraphPill label="final" value={graphState.final} />}
        {typeof graphState.confidence === "number" && (
          <GraphPill label="confidence" value={graphState.confidence.toFixed(2)} />
        )}
        <GraphPill label="nodes" value={String(nodes.length)} />
        <GraphPill label="errors" value={String(errors.length)} tone={errors.length ? "err" : "ok"} />
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-px bg-mc-border text-xs">
        <GraphMetric label="runnable" value={runnable.length} tone="ok" />
        <GraphMetric label="blocked" value={blocked.length} tone={blocked.length ? "warn" : "muted"} />
        <GraphMetric label="failed" value={failed.length} tone={failed.length ? "err" : "muted"} />
        <GraphMetric label="cycles" value={cycles.length} tone={cycles.length ? "err" : "muted"} />
      </div>

      {nodes.length > 0 && (
        <div className="divide-y divide-mc-border">
          {nodes.slice(0, 8).map((node, idx) => (
            <GraphNodeRow key={node.id || node.task_id || idx} node={node} />
          ))}
        </div>
      )}

      {errors.length > 0 && (
        <div className="px-4 py-3 border-t border-mc-border bg-mc-bg/40 space-y-1">
          {errors.slice(0, 4).map((err, idx) => (
            <div key={`${err.task_id || err.node_id || "error"}_${idx}`} className="text-xs text-mc-err">
              {err.kind || "error"} {err.task_id || err.node_id ? `@ ${err.task_id || err.node_id}` : ""}
              {": "}
              {err.message || err.error || "unknown graph error"}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function GraphMetric({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "ok" | "warn" | "err" | "muted";
}) {
  const toneClass =
    tone === "ok"
      ? "text-mc-ok"
      : tone === "warn"
        ? "text-amber-300"
        : tone === "err"
          ? "text-mc-err"
          : "text-mc-muted";

  return (
    <div className="bg-mc-panel/60 px-4 py-3">
      <div className={`text-lg font-semibold ${toneClass}`}>{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-mc-faint">{label}</div>
    </div>
  );
}

function GraphPill({
  label,
  value,
  tone = "muted",
}: {
  label: string;
  value: string;
  tone?: "ok" | "err" | "muted";
}) {
  const toneClass = tone === "ok" ? "text-mc-ok" : tone === "err" ? "text-mc-err" : "text-mc-muted";
  return (
    <span className={`text-[10px] px-2 py-1 rounded bg-mc-bg border border-mc-border ${toneClass}`}>
      {label}: {value}
    </span>
  );
}

function GraphNodeRow({ node }: { node: GraphNode }) {
  const title = node.id || node.task_id || "unnamed";
  const deps = node.dependencies || [];
  const preview = node.description || node.prompt || "";

  return (
    <div className="px-4 py-3 grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_auto] gap-2">
      <div className="min-w-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-xs font-medium text-mc-text truncate">{title}</span>
          {node.status && <StatusBadge status={node.status} />}
        </div>
        {preview && (
          <p className="text-xs text-mc-muted mt-1 line-clamp-2" title={preview}>
            {preview}
          </p>
        )}
      </div>
      <div className="text-[10px] text-mc-faint md:text-right">
        deps: {deps.length ? deps.join(", ") : "none"}
      </div>
    </div>
  );
}

function RoundCard({ round }: { round: Round }) {
  const leader = round.leader;
  const agents = round.agents;

  const ok = agents.filter((a) => a.status === "ok").length;
  const running = agents.filter((a) => a.status === "running").length;
  const err = agents.filter((a) => a.status === "error").length;
  const total = agents.length;

  const maxElapsed = Math.max(
    0,
    ...agents.map((a) => (a.elapsedSeconds ?? 0)),
  );

  return (
    <div className="border border-mc-border rounded-lg bg-mc-panel/40 overflow-hidden">
      {/* 组长卡 */}
      <div className="px-4 py-3 border-b border-mc-border bg-mc-panel/60">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-sm">🎯</span>
            <span className="text-sm font-medium text-mc-text truncate">
              {leader?.goal ? leader.goal : "组长任务拆解"}
            </span>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {leader?.synthesis && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-mc-border text-mc-muted">
                {leader.synthesis}
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3 mt-2 text-xs text-mc-muted">
          <span>{total} 位专家</span>
          {ok > 0 && <span className="text-mc-ok">✓ {ok}</span>}
          {running > 0 && <span className="text-mc-accent">⏳ {running}</span>}
          {err > 0 && <span className="text-mc-err">✗ {err}</span>}
          {maxElapsed > 0 && <span>· {maxElapsed.toFixed(1)}s</span>}
        </div>
      </div>

      {/* 专家卡片墙 */}
      <div className="p-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        {agents.map((agent) => (
          <AgentCard key={agent.id} agent={agent} />
        ))}
      </div>

      {/* 整合结果占位 */}
      {leader && ok + err === total && total > 0 && (
        <div className="px-4 py-2 border-t border-mc-border text-xs text-mc-muted bg-mc-panel/30">
          整合完成 — 结果见聊天时间线
        </div>
      )}
    </div>
  );
}

function AgentCard({ agent }: { agent: Entry }) {
  const [expanded, setExpanded] = useState(false);
  const role = str(agent.roleHint).toLowerCase();
  const meta = ROLE_META[role] || ROLE_META.general;
  const idx = agent.subagentIndex ?? "?";

  return (
    <div className="border border-mc-border rounded-md bg-mc-panel/50 p-3 mc-card">
      <div className="flex items-center gap-2">
        <span className={meta.tone}>{meta.icon}</span>
        <span className={`text-xs font-medium ${meta.tone}`}>{meta.label}</span>
        <span className="text-[10px] text-mc-faint">#{idx}</span>
        <div className="ml-auto">
          <StatusBadge status={agent.status} />
        </div>
      </div>

      <p className="text-xs text-mc-muted mt-2 line-clamp-2" title={str(agent.promptPreview)}>
        {agent.promptPreview || "—"}
      </p>

      <div className="flex items-center gap-3 mt-2 text-[10px] text-mc-faint">
        {typeof agent.hops === "number" && agent.hops > 0 && <span>{agent.hops} hop</span>}
        {typeof agent.elapsedSeconds === "number" && (
          <span>{agent.elapsedSeconds.toFixed(1)}s</span>
        )}
      </div>

      {/* 可展开详情 */}
      {(agent.outputPreview || agent.errorPreview) && (
        <>
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            className="mt-2 text-[10px] text-mc-accent hover:text-mc-accent-dim"
          >
            {expanded ? "收起" : "查看详情"}
          </button>
          {expanded && (
            <div className="mt-2 text-xs text-mc-muted bg-mc-bg/60 rounded p-2 max-h-40 overflow-y-auto">
              {agent.status === "ok" && agent.outputPreview && (
                <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed">
                  {agent.outputPreview}
                </pre>
              )}
              {agent.status === "error" && agent.errorPreview && (
                <pre className="whitespace-pre-wrap break-words font-mono text-[11px] text-mc-err leading-relaxed">
                  {agent.errorPreview}
                </pre>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string | undefined }) {
  if (status === "running") {
    return (
      <span className="inline-flex items-center gap-1 text-[10px] text-mc-accent">
        <span className="w-1.5 h-1.5 rounded-full bg-mc-accent mc-breathe" />
        运行中
      </span>
    );
  }
  if (status === "ok") {
    return <span className="text-[10px] text-mc-ok">✓ 完成</span>;
  }
  if (status === "error") {
    return <span className="text-[10px] text-mc-err">✗ 失败</span>;
  }
  return <span className="text-[10px] text-mc-faint">—</span>;
}
