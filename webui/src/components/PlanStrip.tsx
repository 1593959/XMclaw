// 计划步骤条：plan_* 事件驱动；无显式 plan 时退化为 todo_updated 列表；
// 无 plan/todo 但有正在执行的工具调用时显示「执行中…」指示器。
import { useApp } from "../store/app";

const STEP_CLS: Record<string, string> = {
  pending: "border-mc-border text-mc-faint",
  running: "border-mc-accent/50 bg-mc-accent/10 text-mc-accent",
  done: "border-mc-ok/40 bg-mc-ok/10 text-mc-ok",
  failed: "border-mc-err/40 bg-mc-err/10 text-mc-err",
};

export default function PlanStrip() {
  const plan = useApp((s) => s.chat.plan);
  const todos = useApp((s) => s.chat.todos);
  const entries = useApp((s) => s.chat.entries);

  // 1. plan 步骤条（最高优先级）
  if (plan.steps.length > 0) {
    const done = plan.steps.filter((s) => s.status === "done").length;
    const running = plan.steps.filter((s) => s.status === "running").length;
    return (
      <div className="px-4 py-2 border-b border-mc-border flex items-center gap-1.5 flex-wrap shrink-0">
        <span className="text-xs text-mc-faint mr-1">
          计划 {done}/{plan.steps.length}
        </span>
        {plan.steps.map((s) => (
          <span
            key={s.id}
            title={s.id}
            className={
              "text-[11px] px-2 py-0.5 rounded-full border " + (STEP_CLS[s.status] || STEP_CLS.pending)
            }
          >
            {s.status === "done" ? "✓" : s.status === "running" ? "▶" : s.status === "failed" ? "✗" : "○"}{" "}
            {s.index + 1}
          </span>
        ))}
        {running > 0 && (
          <span className="text-[11px] text-mc-accent animate-pulse">执行中…</span>
        )}
      </div>
    );
  }

  // 2. todo 待办列表
  // todo_write 工具用 "done" 作终态（builtin_memory._VALID_TODO_STATUSES）；
  // 历史代码这里数的是 "completed"，两者对不上 → 标完了进度条永远 0/N。
  // 同时认 done/completed，彻底堵住状态名漂移。
  const isDone = (s: string | undefined) => s === "done" || s === "completed";
  if (todos && todos.items.length > 0) {
    const done = todos.items.filter((t) => isDone(t.status)).length;
    return (
      <div className="px-4 py-2 border-b border-mc-border flex items-center gap-1.5 flex-wrap shrink-0">
        <span className="text-xs text-mc-faint mr-1">
          待办 {done}/{todos.items.length}
        </span>
        {todos.items.slice(0, 8).map((t, i) => (
          <span
            key={i}
            className={
              "text-[11px] px-2 py-0.5 rounded-full border max-w-44 truncate " +
              (isDone(t.status)
                ? STEP_CLS.done
                : t.status === "in_progress"
                  ? STEP_CLS.running
                  : STEP_CLS.pending)
            }
          >
            {String(t.content || "")}
          </span>
        ))}
      </div>
    );
  }

  // 3. fallback：有运行中的工具调用时显示「执行中…」
  const hasRunningTool = entries.some(
    (e) => e.kind === "tool_use" && e.status === "running",
  );
  if (hasRunningTool) {
    return (
      <div className="px-4 py-2 border-b border-mc-border flex items-center gap-1.5 flex-wrap shrink-0">
        <span className="text-[11px] text-mc-accent animate-pulse">执行中…</span>
      </div>
    );
  }

  return null;
}
