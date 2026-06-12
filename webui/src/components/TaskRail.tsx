import { useApp } from "../store/app";
import type { TaskSnapshot } from "../lib/types";

const STATUS_META: Record<TaskSnapshot["status"], { label: string; cls: string }> = {
  running: { label: "运行中", cls: "text-mc-accent" },
  awaiting_input: { label: "等待审批", cls: "text-mc-warn" },
  done: { label: "已完成", cls: "text-mc-ok" },
  failed: { label: "失败", cls: "text-mc-err" },
  chat: { label: "对话", cls: "text-mc-faint" },
};

export default function TaskRail() {
  const tasks = useApp((s) => s.tasks);
  const sids = useApp((s) => s.sids);
  const activeSid = useApp((s) => s.sid);
  const resumeSession = useApp((s) => s.resumeSession);
  const startNewSession = useApp((s) => s.startNewSession);

  // /api/v2/tasks 不可用（旧 daemon）时退化为本地 sid 列表。
  const items: TaskSnapshot[] =
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

  return (
    <aside className="w-56 border-r border-mc-border bg-mc-panel hidden md:flex flex-col shrink-0">
      <div className="flex items-center justify-between px-3 pt-3 pb-2">
        <span className="text-xs text-mc-faint uppercase tracking-wider">任务</span>
        <button
          onClick={startNewSession}
          className="text-xs px-2 py-0.5 rounded border border-mc-border text-mc-muted hover:text-mc-text hover:border-mc-accent/50 cursor-pointer"
          title="新任务 / 新会话"
        >
          + 新建
        </button>
      </div>
      <div className="flex-1 overflow-y-auto px-2 pb-2 space-y-1">
        {items.map((t) => {
          const meta = STATUS_META[t.status] || STATUS_META.chat;
          const active = t.sid === activeSid;
          return (
            <button
              key={t.sid}
              onClick={() => resumeSession(t.sid)}
              className={
                "w-full text-left rounded-md px-2.5 py-2 cursor-pointer border " +
                (active
                  ? "bg-mc-accent/10 border-mc-accent/40"
                  : "border-transparent hover:bg-mc-panel2")
              }
            >
              <div className="text-[13px] font-medium truncate">{t.title || t.sid}</div>
              <div className={"text-xs mt-0.5 " + meta.cls}>
                {t.status === "running" && <span className="animate-pulse">● </span>}
                {meta.label}
                {t.steps_total > 0 && ` · ${t.steps_done}/${t.steps_total}`}
              </div>
            </button>
          );
        })}
        {items.length === 0 && (
          <div className="text-xs text-mc-faint px-2 py-4">暂无任务 — 在下方下达第一条指令</div>
        )}
      </div>
      <DomainNav />
    </aside>
  );
}

// 四域导航（10.M3）：任务是主视图，其余三域是驾驶舱仪表。
const DOMAINS = [
  { key: "tasks", label: "任务", icon: "◧" },
  { key: "memory", label: "记忆", icon: "◔" },
  { key: "skills", label: "能力", icon: "⚡" },
  { key: "system", label: "系统", icon: "⚙" },
] as const;

function DomainNav() {
  const view = useApp((s) => s.view);
  const setView = useApp((s) => s.setView);
  return (
    <div className="shrink-0 border-t border-mc-border grid grid-cols-4">
      {DOMAINS.map((d) => (
        <button
          key={d.key}
          onClick={() => setView(d.key)}
          title={d.label}
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
