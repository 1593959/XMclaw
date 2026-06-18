// Slash 命令面板（移植旧 UI SlashPopover 的核心命令）。输入框首字符为
// "/" 时弹出，↑↓ 选择、Enter/Tab 执行、Esc 关闭。命令分两类：
// 会话动作（本地执行）+ 域跳转（切 view）。

import { useApp } from "../store/app";

export interface SlashCommand {
  cmd: string;
  desc: string;
  run: (api: ReturnType<typeof useApp.getState>) => void;
}

export const SLASH_COMMANDS: SlashCommand[] = [
  { cmd: "/new", desc: "新建任务 / 会话", run: (a) => a.startNewSession() },
  { cmd: "/clear", desc: "清空本地面板（保留 daemon 历史）", run: (a) => a.clearChat() },
  { cmd: "/retry", desc: "回填上一条指令以重发", run: (a) => a.retryLast() },
  { cmd: "/undo", desc: "撤销上一轮对话", run: (a) => a.undoLast() },
  { cmd: "/plan", desc: "切换计划模式", run: (a) => a.togglePlan() },
  { cmd: "/think", desc: "切换深思（ultrathink）", run: (a) => a.toggleUltrathink() },
  { cmd: "/memory", desc: "打开记忆域", run: (a) => a.setView("memory") },
  { cmd: "/skills", desc: "打开能力域", run: (a) => a.setView("skills") },
  { cmd: "/files", desc: "打开文件域（工作区 md 编辑）", run: (a) => a.setView("files") },
  { cmd: "/system", desc: "打开系统域", run: (a) => a.setView("system") },
  { cmd: "/team", desc: "打开专家团", run: (a) => a.setView("team") },
  { cmd: "/tasks", desc: "回到任务视图", run: (a) => a.setView("tasks") },
];

export function matchSlash(draft: string): SlashCommand[] | null {
  if (!draft.startsWith("/")) return null;
  // 已含空格 = 不是纯命令（在写正文），不弹。
  if (draft.includes(" ") || draft.includes("\n")) return null;
  const q = draft.slice(1).toLowerCase();
  return SLASH_COMMANDS.filter((c) => c.cmd.slice(1).startsWith(q));
}

export default function SlashMenu({
  matches,
  active,
  onPick,
}: {
  matches: SlashCommand[];
  active: number;
  onPick: (c: SlashCommand) => void;
}) {
  if (matches.length === 0) return null;
  return (
    <div className="absolute bottom-full left-0 mb-2 w-72 max-h-64 overflow-y-auto rounded-lg border border-mc-border bg-mc-panel shadow-xl z-20">
      {matches.map((c, i) => (
        <button
          key={c.cmd}
          onMouseDown={(e) => {
            e.preventDefault();
            onPick(c);
          }}
          className={
            "w-full text-left px-3 py-2 cursor-pointer flex items-baseline gap-2 " +
            (i === active ? "bg-mc-accent/15" : "hover:bg-mc-panel2")
          }
        >
          <span className="font-mono text-[12.5px] text-mc-accent shrink-0">{c.cmd}</span>
          <span className="text-[11.5px] text-mc-faint truncate">{c.desc}</span>
        </button>
      ))}
    </div>
  );
}
