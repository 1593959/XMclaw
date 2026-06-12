// 活动时间线 — Mission Control 的主舞台。每个 Entry 一行：
// 用户指令 / agent 陈述（含折叠思考块）/ 工具卡 / 审批卡。
// M1 为骨架版：工具卡是通用卡（按类型特化渲染 = 10.M2.1b）。

import { useEffect, useRef, useState } from "react";
import { useApp } from "../store/app";
import type { Entry } from "../lib/types";
import ToolCard, { AgentGroupCard } from "./ToolCards";
import Markdown from "./LazyMarkdown";

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

function RoleMark({ kind }: { kind: "user" | "agent" }) {
  if (kind === "user") {
    return (
      <span className="w-5 h-5 rounded-md bg-mc-panel2 border border-mc-border text-mc-faint text-[10px] flex items-center justify-center shrink-0 mt-0.5 select-none">
        你
      </span>
    );
  }
  return (
    <span className="w-5 h-5 rounded-md bg-gradient-to-br from-mc-accent to-mc-accent-dim text-white text-[10px] font-bold flex items-center justify-center shrink-0 mt-0.5 select-none">
      X
    </span>
  );
}

function AssistantRow({ e }: { e: Entry }) {
  const blocks = e.blocks?.length
    ? e.blocks
    : e.content
      ? [{ type: "text" as const, id: e.id + ":t", content: e.content }]
      : [];
  const streaming = e.status === "streaming";
  const lastTextIdx = (() => {
    for (let i = blocks.length - 1; i >= 0; i--) if (blocks[i].type === "text") return i;
    return -1;
  })();
  const complete = e.status === "complete" || e.status === "error";
  return (
    <div className="flex gap-2.5 group">
      <RoleMark kind="agent" />
      <div className="flex-1 min-w-0 space-y-1.5">
        {e.proactive && (
          <span className="text-[11px] text-mc-accent border border-mc-accent/40 rounded px-1.5">
            主动提议
          </span>
        )}
        {blocks.map((b, i) =>
          b.type === "thinking" ? (
            <ThinkingBlock key={b.id} content={b.content} />
          ) : (
            <div key={b.id} className={streaming && i === lastTextIdx ? "mc-caret" : undefined}>
              <Markdown text={b.content} />
            </div>
          ),
        )}
        {e.status === "thinking" && (
          <div className="flex items-center gap-1.5 text-xs text-mc-faint">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-mc-accent mc-breathe" />
            {e.phase === "calling_llm" ? "正在调用模型…" : "思考中…"}
          </div>
        )}
        {e.status === "cancelled" && <div className="text-xs text-mc-faint">⊘ 已停止</div>}
        {e.status === "error" && !e.content && (
          <div className="text-xs text-mc-err">回合出错</div>
        )}
        {complete && e.content && (
          <div className="flex gap-2 opacity-0 group-hover:opacity-100 transition-opacity">
            <button
              onClick={() => {
                navigator.clipboard?.writeText(e.content);
                useApp.getState().showToast("已复制回复", "ok");
              }}
              className="text-[10.5px] text-mc-faint hover:text-mc-accent cursor-pointer"
            >
              复制
            </button>
            <button
              onClick={() => useApp.getState().retryLast()}
              className="text-[10.5px] text-mc-faint hover:text-mc-accent cursor-pointer"
            >
              重试
            </button>
          </div>
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
  if (e.kind === "security") {
    const high = e.severity === "high" || e.severity === "critical" || e.status === "error";
    return (
      <div className="flex gap-2.5">
        <span className={"text-sm mt-0.5 shrink-0 " + (high ? "text-mc-err" : "text-mc-warn")}>🛡</span>
        <div
          className={
            "flex-1 min-w-0 text-[12.5px] rounded-md border px-3 py-2 " +
            (high
              ? "border-mc-err/40 bg-mc-err/5 text-mc-err"
              : "border-mc-warn/40 bg-mc-warn/5 text-mc-warn")
          }
        >
          {e.content}
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
      <div className="flex gap-2.5 pt-2">
        <RoleMark kind="user" />
        <div className="flex-1 min-w-0 text-[13px] text-mc-text/90 whitespace-pre-wrap leading-relaxed border-l-2 border-mc-border/70 -ml-0.5 pl-2.5">
          {e.content}
          {(e.images || []).length > 0 && (
            <div className="flex gap-2 flex-wrap mt-1.5">
              {e.images!.map((src) => (
                <button
                  key={src}
                  onClick={() => useApp.getState().openLightbox(src, "image")}
                  className="cursor-zoom-in"
                >
                  <img src={src} className="max-h-40 rounded border border-mc-border" alt="附件" />
                </button>
              ))}
            </div>
          )}
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
        <div className="h-full flex flex-col items-center justify-center gap-3 select-none">
          <div className="w-12 h-12 rounded-2xl bg-gradient-to-br from-mc-accent/25 to-mc-accent-dim/10 border border-mc-accent/20 flex items-center justify-center text-xl">
            ◧
          </div>
          <div className="text-mc-muted text-sm font-medium">下达第一条指令</div>
          <div className="text-mc-faint text-xs max-w-72 text-center leading-relaxed">
            计划步骤、工具调用、文件 diff 与审批请求都会在这条时间线上实时展开；产物同步亮在右侧工作区
          </div>
        </div>
      )}
      {entries.map((e) => (
        <div key={e.id} className="mc-rise">
          <Row e={e} />
        </div>
      ))}
    </div>
  );
}
