// 指挥通道：下指令、补充要求、随时打断。

import { useApp } from "../store/app";

export default function Composer() {
  const draft = useApp((s) => s.draft);
  const setDraft = useApp((s) => s.setDraft);
  const sendUser = useApp((s) => s.sendUser);
  const cancelTurn = useApp((s) => s.cancelTurn);
  const busy = useApp((s) => !!s.chat.pendingAssistantId);
  const connected = useApp((s) => s.connection.status === "connected");

  function submit() {
    if (draft.trim()) sendUser(draft);
  }

  return (
    <div className="px-4 py-3 border-t border-mc-border shrink-0">
      <div className="flex items-end gap-2 border border-mc-border rounded-lg bg-mc-panel2 px-3 py-2 focus-within:border-mc-accent/60">
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
          placeholder={connected ? "下达指令、追加要求，或随时打断…（Enter 发送 / Esc 打断）" : "等待连接 daemon…"}
          rows={Math.min(6, Math.max(1, draft.split("\n").length))}
          className="flex-1 bg-transparent outline-none resize-none text-[13px] leading-relaxed placeholder:text-mc-faint"
        />
        {busy && (
          <button
            onClick={cancelTurn}
            className="text-xs px-2.5 py-1 rounded border border-mc-err/40 text-mc-err hover:bg-mc-err/10 cursor-pointer shrink-0"
            title="打断当前回合 (Esc)"
          >
            ■ 停止
          </button>
        )}
        <button
          onClick={submit}
          disabled={!draft.trim()}
          className="text-xs px-3 py-1 rounded bg-mc-accent text-white disabled:opacity-30 hover:bg-mc-accent-dim cursor-pointer disabled:cursor-default shrink-0"
        >
          发送
        </button>
      </div>
    </div>
  );
}
