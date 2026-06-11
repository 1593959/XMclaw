import { useApp } from "../store/app";

const STATUS_LABEL: Record<string, string> = {
  connected: "在线",
  connecting: "连接中",
  reconnecting: "重连中",
  disconnected: "离线",
  auth_failed: "认证失败",
  superseded: "已被其他标签页接管",
};

export default function Hud() {
  const connection = useApp((s) => s.connection);
  const usage = useApp((s) => s.chat.tokenUsage);
  const hud = useApp((s) => s.hud);
  const ok = connection.status === "connected";
  return (
    <header className="flex items-center gap-3 px-4 h-11 border-b border-mc-border bg-mc-panel shrink-0">
      <span className="font-semibold tracking-wide">XMclaw</span>
      <span className="text-xs px-2 py-0.5 rounded-full bg-mc-accent/15 text-mc-accent">
        {usage?.last_model || (hud?.model as string) || "Mission Control"}
      </span>
      <div className="flex-1" />
      {usage && (
        <span className="text-xs text-mc-muted" title="本会话累计花费">
          ${usage.spent_usd.toFixed(2)}
        </span>
      )}
      {hud?.memory_facts != null && (
        <span className="text-xs text-mc-muted">记忆 {String(hud.memory_facts)}</span>
      )}
      <span
        className={"text-xs flex items-center gap-1.5 " + (ok ? "text-mc-ok" : "text-mc-warn")}
        title={connection.lastError || ""}
      >
        <span
          className={
            "inline-block w-2 h-2 rounded-full " +
            (ok ? "bg-mc-ok" : connection.status === "disconnected" ? "bg-mc-err" : "bg-mc-warn")
          }
        />
        {STATUS_LABEL[connection.status] || connection.status}
        {connection.status === "reconnecting" && ` (${connection.attempt})`}
      </span>
    </header>
  );
}
