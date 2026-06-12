// 指挥通道：下指令、补充要求、随时打断。
// 2026-06-12 打磨轮：补回 plan 模式 / ultrathink / 模型 profile 切换
// （WS 帧字段与旧 UI 一致：plan_mode / ultrathink / llm_profile_id）。

import { useApp } from "../store/app";

function Chip({
  active,
  onClick,
  children,
  title,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  title: string;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={
        "text-[11px] px-2.5 py-1 rounded-full border cursor-pointer select-none " +
        (active
          ? "border-mc-accent/60 text-mc-accent bg-mc-accent/10"
          : "border-mc-border text-mc-faint hover:text-mc-muted hover:border-mc-border")
      }
    >
      {children}
    </button>
  );
}

export default function Composer() {
  const draft = useApp((s) => s.draft);
  const setDraft = useApp((s) => s.setDraft);
  const sendUser = useApp((s) => s.sendUser);
  const cancelTurn = useApp((s) => s.cancelTurn);
  const busy = useApp((s) => !!s.chat.pendingAssistantId);
  const connected = useApp((s) => s.connection.status === "connected");
  const planMode = useApp((s) => s.planMode);
  const ultrathink = useApp((s) => s.ultrathink);
  const togglePlan = useApp((s) => s.togglePlan);
  const toggleUltrathink = useApp((s) => s.toggleUltrathink);
  const profiles = useApp((s) => s.profiles);
  const llmProfileId = useApp((s) => s.llmProfileId);
  const setLlmProfile = useApp((s) => s.setLlmProfile);

  function submit() {
    if (draft.trim()) sendUser(draft);
  }

  return (
    <div className="px-4 pt-2 pb-3 border-t border-mc-border shrink-0 bg-mc-panel/40">
      <div className="flex items-center gap-1.5 mb-2">
        <Chip active={planMode} onClick={togglePlan} title="先出计划，你批准后再执行">
          ☰ 计划模式
        </Chip>
        <Chip active={ultrathink} onClick={toggleUltrathink} title="延长思考预算，难题更稳">
          ✦ 深思
        </Chip>
        {profiles.length > 0 && (
          <select
            value={llmProfileId}
            onChange={(e) => setLlmProfile(e.target.value)}
            title="本会话使用的模型 profile"
            className="text-[11px] px-2 py-1 rounded-full border border-mc-border bg-mc-panel text-mc-muted cursor-pointer outline-none hover:border-mc-accent/40 max-w-44"
          >
            <option value="">默认模型</option>
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label || p.id} · {p.model}
              </option>
            ))}
          </select>
        )}
        <div className="flex-1" />
        {busy && (
          <span className="text-[10.5px] text-mc-faint">
            Esc 打断 · 输入可追加指令
          </span>
        )}
      </div>
      <div className="flex items-end gap-2 border border-mc-border rounded-xl bg-mc-panel2 px-3.5 py-2.5 focus-within:border-mc-accent/60 focus-within:shadow-[0_0_0_3px_rgba(139,92,246,0.08)] transition-shadow">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
            if (e.key === "Escape" && busy) cancelTurn();
          }}
          placeholder={
            connected
              ? planMode
                ? "描述目标 — agent 会先给出计划等你批准…"
                : "下达指令、追加要求，或随时打断…"
              : "等待连接 daemon…"
          }
          rows={Math.min(6, Math.max(1, draft.split("\n").length))}
          className="flex-1 bg-transparent outline-none resize-none text-[13px] leading-relaxed placeholder:text-mc-faint"
        />
        {busy && (
          <button
            onClick={cancelTurn}
            className="text-xs px-3 py-1.5 rounded-lg border border-mc-err/40 text-mc-err hover:bg-mc-err/10 cursor-pointer shrink-0"
            title="打断当前回合 (Esc)"
          >
            ■ 停止
          </button>
        )}
        <button
          onClick={submit}
          disabled={!draft.trim()}
          className="text-xs px-3.5 py-1.5 rounded-lg bg-mc-accent text-white font-medium disabled:opacity-30 hover:bg-mc-accent-dim cursor-pointer disabled:cursor-default shrink-0"
        >
          发送 ↵
        </button>
      </div>
    </div>
  );
}
