// 指挥通道：下指令、补充要求、随时打断。
// 2026-06-12 打磨轮：补回 plan 模式 / ultrathink / 模型 profile 切换
// （WS 帧字段与旧 UI 一致：plan_mode / ultrathink / llm_profile_id）。

import { useRef, useState } from "react";
import { useApp } from "../store/app";
import SlashMenu, { matchSlash, type SlashCommand } from "./SlashMenu";

function fileIcon(mime: string): string {
  if (mime.startsWith("audio/")) return "🎵";
  if (mime.startsWith("video/")) return "🎬";
  if (mime === "application/pdf") return "📕";
  if (mime.startsWith("text/") || mime.includes("json") || mime.includes("xml")) return "📄";
  if (mime.includes("zip") || mime.includes("compressed")) return "🗜";
  return "📎";
}

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
  const teamMode = useApp((s) => s.teamMode);
  const toggleTeam = useApp((s) => s.toggleTeam);
  const profiles = useApp((s) => s.profiles);
  const llmProfileId = useApp((s) => s.llmProfileId);
  const setLlmProfile = useApp((s) => s.setLlmProfile);
  const attachments = useApp((s) => s.attachments);
  const addAttachments = useApp((s) => s.addAttachments);
  const removeAttachment = useApp((s) => s.removeAttachment);
  const fileRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const [slashIdx, setSlashIdx] = useState(0);
  const slashMatches = matchSlash(draft);

  function runSlash(c: SlashCommand) {
    setDraft("");
    setSlashIdx(0);
    c.run(useApp.getState());
  }

  function submit() {
    // 纯命令（完整匹配某条）→ 执行而非发送。
    if (slashMatches && slashMatches.length > 0) {
      const exact = slashMatches.find((c) => c.cmd === draft.trim());
      runSlash(exact || slashMatches[slashIdx] || slashMatches[0]);
      return;
    }
    if (draft.trim() || attachments.length > 0) sendUser(draft);
  }

  function onPaste(e: React.ClipboardEvent) {
    const files = Array.from(e.clipboardData.files);
    if (files.length > 0) {
      e.preventDefault();
      addAttachments(files);
    }
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
        <Chip active={teamMode} onClick={toggleTeam} title="强制派专家团并行执行（swarm 模式）">
          👥 专家团
        </Chip>
        {profiles.length > 0 && (
          <select
            value={llmProfileId || profiles[0].id}
            onChange={(e) => setLlmProfile(e.target.value)}
            title="本会话使用的模型"
            className="text-[11px] px-2 py-1 rounded-full border border-mc-border bg-mc-panel text-mc-muted cursor-pointer outline-none hover:border-mc-accent/40 max-w-44"
          >
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
      {attachments.length > 0 && (
        <div className="flex gap-2 flex-wrap mb-2">
          {attachments.map((a, i) => (
            <div key={i} className="relative group">
              {a.channel === "image" ? (
                <img
                  src={a.dataUrl}
                  alt={a.name}
                  className="h-16 w-16 object-cover rounded-md border border-mc-border"
                />
              ) : (
                <div className="h-16 w-28 rounded-md border border-mc-border bg-mc-panel2 flex flex-col items-center justify-center px-1.5">
                  <span className="text-lg leading-none">{fileIcon(a.mime)}</span>
                  <span className="text-[9px] text-mc-faint truncate max-w-full mt-0.5" title={a.name}>
                    {a.name}
                  </span>
                </div>
              )}
              <button
                onClick={() => removeAttachment(i)}
                className="absolute -top-1.5 -right-1.5 w-4 h-4 rounded-full bg-mc-err text-white text-[10px] flex items-center justify-center cursor-pointer opacity-0 group-hover:opacity-100"
                aria-label="移除"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          if (e.dataTransfer.files.length > 0) addAttachments(e.dataTransfer.files);
        }}
        className={
          "relative flex items-end gap-2 border rounded-xl bg-mc-panel2 px-3.5 py-2.5 transition-shadow " +
          (dragOver
            ? "border-mc-accent border-dashed shadow-[0_0_0_3px_rgba(139,92,246,0.12)]"
            : "border-mc-border focus-within:border-mc-accent/60 focus-within:shadow-[0_0_0_3px_rgba(139,92,246,0.08)]")
        }
      >
        <input
          ref={fileRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            if (e.target.files) addAttachments(e.target.files);
            e.target.value = "";
          }}
        />
        <button
          onClick={() => fileRef.current?.click()}
          title="添加图片 / 文档 / 音视频（也可直接粘贴 / 拖拽到此）"
          className="text-mc-faint hover:text-mc-accent cursor-pointer shrink-0 text-lg leading-none mb-0.5"
          aria-label="添加附件"
        >
          ＋
        </button>
        {slashMatches && slashMatches.length > 0 && (
          <SlashMenu matches={slashMatches} active={slashIdx} onPick={runSlash} />
        )}
        <textarea
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            setSlashIdx(0);
          }}
          onPaste={onPaste}
          onKeyDown={(e) => {
            const sm = slashMatches;
            if (sm && sm.length > 0) {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setSlashIdx((i) => (i + 1) % sm.length);
                return;
              }
              if (e.key === "ArrowUp") {
                e.preventDefault();
                setSlashIdx((i) => (i - 1 + sm.length) % sm.length);
                return;
              }
              if (e.key === "Tab") {
                e.preventDefault();
                runSlash(sm[slashIdx] || sm[0]);
                return;
              }
              if (e.key === "Escape") {
                e.preventDefault();
                setDraft("");
                return;
              }
            }
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
            if (e.key === "Escape" && busy) cancelTurn();
          }}
          placeholder={
            dragOver
              ? "松手添加图片…"
              : connected
                ? planMode
                  ? "描述目标 — agent 会先给出计划等你批准…"
                  : "下达指令、追加要求，或粘贴/拖拽图片…"
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
