// 专家团视图（P0）：把 fanout / subagent 事件重组为结构化团队看板。
// 组长拆解 → 专家并行 → 整合。Mission Control 风格，深空墨底。

import { useMemo, useState } from "react";
import { useApp } from "../store/app";
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
  const rounds = useMemo(() => groupRounds(entries), [entries]);

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
            onClick={() => setExpanded((v) => !v)}
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
