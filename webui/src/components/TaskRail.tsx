import { useState } from "react";
import { useApp } from "../store/app";
import type { TaskSnapshot } from "../lib/types";

const STATUS_META: Record<TaskSnapshot["status"], { label: string; cls: string; bar: string }> = {
  running: { label: "运行中", cls: "text-mc-accent", bar: "#8b5cf6" },
  awaiting_input: { label: "等你回答", cls: "text-mc-warn", bar: "#fbbf24" },
  done: { label: "已完成", cls: "text-mc-ok", bar: "#34d399" },
  failed: { label: "失败", cls: "text-mc-err", bar: "#f87171" },
  chat: { label: "对话", cls: "text-mc-faint", bar: "transparent" },
};

function relTime(ts: number): string {
  if (!ts) return "";
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}

const ACTIVITY_LABEL: Record<string, string> = {
  tool_call_emitted: "调用工具",
  tool_invocation_finished: "工具返回",
  llm_request: "思考中",
  llm_response: "已回复",
  plan_step_started: "执行计划步骤",
  agent_asked_question: "等你拍板",
  user_message: "收到指令",
  todo_updated: "更新待办",
};

export default function TaskRail({ width }: { width?: number }) {
  const tasks = useApp((s) => s.tasks);
  const sids = useApp((s) => s.sids);
  const activeSid = useApp((s) => s.sid);
  const resumeSession = useApp((s) => s.resumeSession);
  const startNewSession = useApp((s) => s.startNewSession);
  const deleteSession = useApp((s) => s.deleteSession);
  const clearSessions = useApp((s) => s.clearSessions);
  const [query, setQuery] = useState("");
  const [confirmSid, setConfirmSid] = useState<string | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);

  // /api/v2/tasks 不可用（旧 daemon）时退化为本地 sid 列表。
  const all: TaskSnapshot[] =
    tasks.length > 0
      ? tasks
      : sids.map((sid) => ({
          sid,
          title: sid.slice(0, 14),
          status: "chat" as const,
          steps_total: 0,
          steps_done: 0,
          updated_at: 0,
          last_activity: "",
        }));
  const q = query.trim().toLowerCase();
  const items = q ? all.filter((t) => (t.title || t.sid).toLowerCase().includes(q)) : all;

  return (
    <aside
      style={width ? { width } : undefined}
      className="w-56 border-r border-mc-border bg-mc-panel hidden md:flex flex-col shrink-0"
    >
      <div className="flex items-center justify-between px-3 pt-3 pb-2">
        <span className="text-xs text-mc-faint uppercase tracking-wider">任务</span>
        <div className="flex items-center gap-1 relative">
          {/* 批量清除：一键删已结束 / 全部，不用逐个 hover×。 */}
          <button
            onClick={() => setMenuOpen((v) => !v)}
            className="text-xs px-1.5 py-0.5 rounded border border-mc-border text-mc-faint hover:text-mc-err hover:border-mc-err/50 cursor-pointer"
            title="批量清除会话"
            aria-label="批量清除会话"
          >
            🧹
          </button>
          {menuOpen && (
            <>
              <div className="fixed inset-0 z-10" onClick={() => setMenuOpen(false)} />
              <div className="absolute right-0 top-7 z-20 w-40 rounded-md border border-mc-border bg-mc-panel2 shadow-lg py-1 text-[12px]">
                <button
                  onClick={() => { clearSessions("finished"); setMenuOpen(false); }}
                  className="w-full text-left px-3 py-1.5 hover:bg-mc-panel cursor-pointer"
                >
                  清除已结束会话
                </button>
                <button
                  onClick={() => {
                    if (window.confirm("删除除当前会话外的所有会话？不可恢复。")) {
                      clearSessions("all");
                    }
                    setMenuOpen(false);
                  }}
                  className="w-full text-left px-3 py-1.5 text-mc-err hover:bg-mc-panel cursor-pointer"
                >
                  全部删除（保留当前）
                </button>
              </div>
            </>
          )}
          <button
            onClick={startNewSession}
            className="text-xs px-2 py-0.5 rounded border border-mc-border text-mc-muted hover:text-mc-text hover:border-mc-accent/50 cursor-pointer"
            title="新任务 / 新会话"
          >
            + 新建
          </button>
        </div>
      </div>
      {all.length > 6 && (
        <div className="px-2 pb-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索任务…"
            className="w-full text-[11.5px] px-2.5 py-1.5 rounded-md border border-mc-border bg-mc-panel2 outline-none focus:border-mc-accent placeholder:text-mc-faint"
          />
        </div>
      )}
      <div className="flex-1 overflow-y-auto px-2 pb-2 space-y-1">
        {items.map((t) => {
          const meta = STATUS_META[t.status] || STATUS_META.chat;
          const active = t.sid === activeSid;
          const activity = ACTIVITY_LABEL[t.last_activity] || "";
          return (
            <div
              key={t.sid}
              role="button"
              tabIndex={0}
              onClick={() => resumeSession(t.sid)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") resumeSession(t.sid);
              }}
              style={{ "--mc-task-accent": meta.bar } as React.CSSProperties}
              className={
                "mc-task-card mc-card group relative w-full text-left rounded-md pl-3 pr-2.5 py-2 cursor-pointer border " +
                (active
                  ? "bg-mc-accent/10 border-mc-accent/40"
                  : "border-mc-border/60 bg-mc-panel2/30 hover:bg-mc-panel2")
              }
            >
              <div className="flex items-start gap-1">
                <div className="text-[12.5px] font-medium truncate leading-snug flex-1">
                  {t.title || t.sid}
                </div>
                {confirmSid === t.sid ? (
                  <span className="flex gap-1 shrink-0" onClick={(e) => e.stopPropagation()}>
                    <button
                      onClick={() => {
                        deleteSession(t.sid);
                        setConfirmSid(null);
                      }}
                      className="text-[10px] text-mc-err hover:underline cursor-pointer"
                    >
                      删除
                    </button>
                    <button
                      onClick={() => setConfirmSid(null)}
                      className="text-[10px] text-mc-faint hover:text-mc-muted cursor-pointer"
                    >
                      取消
                    </button>
                  </span>
                ) : (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setConfirmSid(t.sid);
                    }}
                    className="text-mc-faint hover:text-mc-err text-xs leading-none shrink-0 opacity-0 group-hover:opacity-100 cursor-pointer"
                    title="删除会话"
                    aria-label="删除会话"
                  >
                    ×
                  </button>
                )}
              </div>
              <div className="flex items-center gap-1.5 mt-1">
                <span className={"text-[11px] " + meta.cls}>
                  {t.status === "running" && (
                    <span className="inline-block w-1.5 h-1.5 rounded-full bg-mc-accent mc-breathe mr-1 align-middle" />
                  )}
                  {meta.label}
                </span>
                {t.steps_total > 0 && (
                  <span className="text-[10.5px] text-mc-faint tabular-nums">
                    {t.steps_done}/{t.steps_total}
                  </span>
                )}
                <span className="flex-1" />
                <span className="text-[10px] text-mc-faint">{relTime(t.updated_at)}</span>
              </div>
              {t.status === "running" && activity && (
                <div className="text-[10.5px] text-mc-faint mt-0.5 truncate">{activity}…</div>
              )}
              {!!t.artifacts?.length && (
                <div className="text-[10.5px] text-mc-faint mt-1 truncate">
                  <span className="text-mc-muted">产物 ×{t.artifacts.length}</span>
                  <span className="mx-1">·</span>
                  <span>{artifactLabel(t.artifacts[0])}</span>
                </div>
              )}
              {!!t.strategy_signals?.length && (
                <div className="text-[10.5px] text-mc-warn mt-1 truncate">
                  {strategySignalLabel(t.strategy_signals[0])}
                </div>
              )}
              {t.steps_total > 0 && (
                <div className="h-0.5 rounded-full bg-mc-border mt-1.5">
                  <div
                    className="h-0.5 rounded-full transition-all duration-500"
                    style={{
                      width: `${Math.round((t.steps_done / t.steps_total) * 100)}%`,
                      background: meta.bar,
                    }}
                  />
                </div>
              )}
            </div>
          );
        })}
        {items.length === 0 && (
          <div className="text-xs text-mc-faint px-2 py-4">
            {q ? "没有匹配的任务" : "暂无任务 — 在下方下达第一条指令"}
          </div>
        )}
      </div>
      <DomainNav />
    </aside>
  );
}

function artifactLabel(a: NonNullable<TaskSnapshot["artifacts"]>[number]): string {
  const name = a.name || a.path?.split(/[\\/]/).pop() || a.url?.split("/").pop() || "未命名产物";
  const drive = a.target_drive ? `${a.target_drive} ` : "";
  const kind = artifactKindLabel(a.artifact_type || "file");
  return `${drive}${kind} ${name}`;
}

function artifactKindLabel(kind: string): string {
  const labels: Record<string, string> = {
    image: "图片",
    video: "视频",
    audio: "音频",
    document: "文档",
    installer: "安装包",
    archive: "压缩包",
    file: "文件",
  };
  return labels[kind] || kind;
}

function strategySignalLabel(s: NonNullable<TaskSnapshot["strategy_signals"]>[number]): string {
  const decision = s.strategy_decision === "ask_user" ? "需要询问" : "已要求换策略";
  const kind = s.kind === "no_progress" ? "无进展" : s.kind === "stuck_loop" ? "卡住循环" : s.kind || "失败";
  return `${decision} · ${kind}`;
}

// 五域导航（10.M3 + 2026-06-17 文件域）：任务是主视图，其余为驾驶舱仪表。
const DOMAINS = [
  { key: "tasks", label: "任务", icon: "◧" },
  { key: "memory", label: "记忆", icon: "◔" },
  { key: "skills", label: "能力", icon: "⚡" },
  { key: "files", label: "文件", icon: "🗂" },
  { key: "team", label: "专家团", icon: "👥" },
  { key: "control", label: "控制", icon: "C" },
  { key: "system", label: "系统", icon: "⚙" },
] as const;

export function DomainNav({ className = "" }: { className?: string }) {
  const view = useApp((s) => s.view);
  const setView = useApp((s) => s.setView);
  return (
    <div className={"shrink-0 border-t border-mc-border grid grid-cols-7 " + className}>
      {DOMAINS.map((d) => (
        <button
          key={d.key}
          onClick={() => setView(d.key)}
          data-domain={d.key}
          title={d.label}
          aria-label={d.label}
          aria-current={view === d.key ? "page" : undefined}
          className={
            "py-2 text-center cursor-pointer " +
            (view === d.key ? "text-mc-accent bg-mc-accent/10" : "text-mc-faint hover:text-mc-muted")
          }
        >
          <div className="text-sm leading-none">{d.icon}</div>
          <div className="text-[10px] mt-0.5">{d.label}</div>
        </button>
      ))}
    </div>
  );
}
