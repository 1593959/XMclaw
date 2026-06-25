import { useState } from "react";

// 折叠的「思考过程」块。两处复用：
//   1) Timeline — LLM 原生 extended-thinking(llm_thinking_chunk) 累积的 thinking
//   2) ToolCards — ``think`` 工具的 args.thought（深思模式强制的可审计推理）
// 用户明确要求 think 走这个「思考过程」样式，而不是普通工具卡（"💭 思考"）。
export default function ThinkingBlock({ content }: { content: string }) {
  const [open, setOpen] = useState(false);
  if (!content) return null;
  return (
    <div className="border-l-2 border-mc-border pl-2">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="text-[11px] text-mc-faint cursor-pointer hover:text-mc-muted"
      >
        {open ? "▾ 思考过程" : "▸ 思考过程"}
      </button>
      {open && (
        <div className="text-xs text-mc-faint whitespace-pre-wrap mt-1">{content}</div>
      )}
    </div>
  );
}
