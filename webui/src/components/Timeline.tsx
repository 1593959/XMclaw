// 活动时间线 — Mission Control 的主舞台。每个 Entry 一行：
// 用户指令 / agent 陈述（含折叠思考块）/ 工具卡 / 审批卡。
// M1 为骨架版：工具卡是通用卡（按类型特化渲染 = 10.M2.1b）。

import { useEffect, useRef, useState } from "react";
import { useApp } from "../store/app";
import type { Entry } from "../lib/types";
import ToolCard, { AgentGroupCard } from "./ToolCards";
import Markdown from "../lib/Markdown";

function QuestionCard({ e }: { e: Entry }) {
  const answerQuestion = useApp((s) => s.answerQuestion);
  const [other, setOther] = useState("");
  const q = e.question!;
  const answered = e.status === "complete";
  return (
    <div className="border border-mc-warn/40 bg-mc-warn/5 rounded-md px-3 py-2.5">
      <div className="text-[13px] font-medium text-mc-warn mb-2">{q.question}</div>
      {answered ? (
        <div className="text-xs text-mc-muted">已回答：{String(e.answer ?? "")}</div>
      ) : (
        <div className="flex flex-wrap gap-2">
          {q.options.map((opt) => (
            <button
              key={opt}
              onClick={() => answerQuestion(q.id, opt)}
              className="text-xs px-3 py-1 rounded border border-mc-border hover:border-mc-warn/60 hover:bg-mc-warn/10 cursor-pointer"
            >
              {opt}
            </button>
          ))}
          {q.allow_other && (
            <form
              onSubmit={(ev) => {
                ev.preventDefault();
                if (other.trim()) answerQuestion(q.id, other.trim());
              }}
              className="flex gap-1"
            >
              <input
                value={other}
                onChange={(ev) => setOther(ev.target.value)}
                placeholder="其他…"
                className="text-xs px-2 py-1 rounded border border-mc-border bg-mc-panel outline-none focus:border-mc-accent w-36"
              />
            </form>
          )}
        </div>
      )}
    </div>
  );
}

function ThinkingBlock({ content }: { content: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-l-2 border-mc-border pl-2">
      <button
        onClick={() => setOpen(!open)}
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

function AssistantRow({ e }: { e: Entry }) {
  const blocks = e.blocks?.length
    ? e.blocks
    : e.content
      ? [{ type: "text" as const, id: e.id + ":t", content: e.content }]
      : [];
  return (
    <div className="flex gap-2.5">
      <span className="text-mc-accent text-sm mt-0.5 shrink-0">◆</span>
      <div className="flex-1 min-w-0 space-y-1.5">
        {e.proactive && (
          <span className="text-[11px] text-mc-accent border border-mc-accent/40 rounded px-1.5">
            主动提议
          </span>
        )}
        {blocks.map((b) =>
          b.type === "thinking" ? (
            <ThinkingBlock key={b.id} content={b.content} />
          ) : (
            <Markdown key={b.id} text={b.content} />
          ),
        )}
        {e.status === "thinking" && (
          <div className="text-xs text-mc-faint animate-pulse">
            {e.phase === "calling_llm" ? "正在调用模型…" : "思考中…"}
          </div>
        )}
        {e.status === "cancelled" && <div className="text-xs text-mc-faint">已停止</div>}
        {e.status === "error" && !e.content && (
          <div className="text-xs text-mc-err">回合出错</div>
        )}
      </div>
    </div>
  );
}

function Row({ e }: { e: Entry }) {
  // 空鬼泡守卫（对位旧 UI B-220）：乐观 thinking 占位的 correlation_id
  // 与 daemon 的 turn id 不同时，占位被 finalizeAbandoned 收尾成空 complete
  // 条目 — 渲染层直接吞掉，不显示空 ◆ 行。
  if (
    e.role === "assistant" &&
    !e.kind &&
    e.status === "complete" &&
    !e.content &&
    !e.blocks?.length &&
    !e.thinking
  ) {
    return null;
  }
  if (e.kind === "tool_use") {
    return (
      <div className="flex gap-2.5">
        <span className="text-mc-faint text-sm mt-1 shrink-0">⚙</span>
        <div className="flex-1 min-w-0">
          <ToolCard e={e} />
        </div>
      </div>
    );
  }
  if (e.kind === "question") {
    return (
      <div className="flex gap-2.5">
        <span className="text-mc-warn text-sm mt-1 shrink-0">⚠</span>
        <div className="flex-1 min-w-0">
          <QuestionCard e={e} />
        </div>
      </div>
    );
  }
  if (e.kind === "worker" || e.kind === "subagent") {
    return (
      <div className="flex gap-2.5">
        <span className="text-mc-accent text-sm mt-1 shrink-0">⛓</span>
        <div className="flex-1 min-w-0">
          <AgentGroupCard e={e} />
        </div>
      </div>
    );
  }
  if (e.role === "user") {
    return (
      <div className="flex gap-2.5">
        <span className="text-mc-faint text-sm mt-0.5 shrink-0">›</span>
        <div className="flex-1 text-[13px] text-mc-muted whitespace-pre-wrap">
          {e.content}
          {(e.images || []).map((src) => (
            <img key={src} src={src} className="max-w-xs rounded mt-1 border border-mc-border" />
          ))}
        </div>
      </div>
    );
  }
  return <AssistantRow e={e} />;
}

export default function Timeline() {
  const entries = useApp((s) => s.chat.entries);
  const ref = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  // 贴底自动滚动；用户上翻后不再打扰。
  useEffect(() => {
    const el = ref.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [entries]);

  return (
    <div
      ref={ref}
      onScroll={() => {
        const el = ref.current;
        if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
      }}
      className="flex-1 overflow-y-auto px-4 py-3 space-y-3"
    >
      {entries.length === 0 && (
        <div className="h-full flex items-center justify-center text-mc-faint text-sm">
          下达一条指令开始 — agent 的计划、工具调用与产物都会在这里实时展开
        </div>
      )}
      {entries.map((e) => (
        <Row key={e.id} e={e} />
      ))}
    </div>
  );
}
