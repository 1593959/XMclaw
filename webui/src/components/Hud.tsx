import { useApp } from "../store/app";

const STATUS_LABEL: Record<string, string> = {
  connected: "在线",
  connecting: "连接中",
  reconnecting: "重连中",
  disconnected: "离线",
  auth_failed: "认证失败",
  superseded: "已被其他标签页接管",
};

function Metric({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="flex items-baseline gap-1.5 px-3 border-l border-mc-border first:border-l-0">
      <span className="text-[10.5px] text-mc-faint">{label}</span>
      <span className={"text-xs font-medium tabular-nums " + (accent ? "text-mc-accent" : "text-mc-text")}>
        {value}
      </span>
    </div>
  );
}

export default function Hud() {
  const connection = useApp((s) => s.connection);
  const usage = useApp((s) => s.chat.tokenUsage);
  const hud = useApp((s) => s.hud);
  const busy = useApp((s) => !!s.chat.pendingAssistantId);
  const ok = connection.status === "connected";

  return (
    <header className="flex items-center gap-3 px-4 h-12 border-b border-mc-border bg-mc-panel shrink-0">
      <div className="flex items-center gap-2.5">
        <div className="mc-logo">X</div>
        <div className="leading-tight">
          <div className="text-[13px] font-semibold tracking-wide">XMclaw</div>
          <div className="text-[10px] text-mc-faint -mt-0.5">Mission Control</div>
        </div>
      </div>

      {busy && (
        <span className="flex items-center gap-1.5 text-[11px] text-mc-accent ml-2">
          <span className="inline-block w-1.5 h-1.5 rounded-full bg-mc-accent mc-breathe" />
          执行中
        </span>
      )}

      <div className="flex-1" />

      <div className="hidden sm:flex items-center">
        {(usage?.last_model || hud?.model) && (
          <Metric label="模型" value={String(usage?.last_model || hud?.model)} accent />
        )}
        {usage && <Metric label="花费" value={`$${usage.spent_usd.toFixed(2)}`} />}
        {usage && (
          <Metric
            label="tokens"
            value={`${((usage.prompt_tokens + usage.completion_tokens) / 1000).toFixed(1)}k`}
          />
        )}
        {hud?.memory_facts != null && <Metric label="记忆" value={String(hud.memory_facts)} />}
      </div>

      <span
        className={
          "flex items-center gap-1.5 text-[11px] px-2.5 py-1 rounded-full border " +
          (ok
            ? "border-mc-ok/30 text-mc-ok bg-mc-ok/5"
            : connection.status === "disconnected" || connection.status === "auth_failed"
              ? "border-mc-err/30 text-mc-err bg-mc-err/5"
              : "border-mc-warn/30 text-mc-warn bg-mc-warn/5")
        }
        title={connection.lastError || ""}
      >
        <span
          className={
            "inline-block w-1.5 h-1.5 rounded-full " +
            (ok ? "bg-mc-ok" : connection.status === "disconnected" ? "bg-mc-err" : "bg-mc-warn animate-pulse")
          }
        />
        {STATUS_LABEL[connection.status] || connection.status}
        {connection.status === "reconnecting" && ` (${connection.attempt})`}
      </span>
    </header>
  );
}
