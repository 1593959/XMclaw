// 轻量 toast（slash 命令 / 消息操作的反馈）。单条、自动消失、底部居中。

import { useApp } from "../store/app";

const TONE: Record<string, string> = {
  info: "border-mc-border text-mc-text",
  ok: "border-mc-ok/40 text-mc-ok bg-mc-ok/5",
  err: "border-mc-err/40 text-mc-err bg-mc-err/5",
};

export default function Toast() {
  const toast = useApp((s) => s.toast);
  if (!toast) return null;
  return (
    <div className="fixed bottom-24 left-1/2 -translate-x-1/2 z-40 pointer-events-none mc-rise">
      <div
        className={
          "px-3.5 py-2 rounded-lg border bg-mc-panel text-[12.5px] shadow-lg " +
          (TONE[toast.tone] || TONE.info)
        }
      >
        {toast.text}
      </div>
    </div>
  );
}
