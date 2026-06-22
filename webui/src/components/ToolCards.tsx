// 工具卡按类型特化渲染（设计规格 §2.3.1，10.M2.1b）。
// apply_patch/file_write → 内联红绿 diff 卡（行号 + +N −M + 预览切换）
// bash → 终端式卡；file_read/glob/grep → 单行摘要卡；
// 带截图的结果 → 缩略卡；worker/subagent → 可折叠执行组；其余 → JSON 卡。

import { useMemo, useState } from "react";
import type { Entry } from "../lib/types";
import { buildDiffFromStrings, collapseMiddle, type DiffLine, type DiffStat } from "../lib/difflines";
import { useApp } from "../store/app";
import ThinkingBlock from "./ThinkingBlock";

const str = (v: unknown): string => (typeof v === "string" ? v : "");

function StatusDot({ status }: { status: Entry["status"] }) {
  if (status === "running")
    return <span className="text-mc-accent animate-pulse text-xs">●</span>;
  if (status === "error") return <span className="text-mc-err text-xs">✗</span>;
  return <span className="text-mc-ok text-xs">✓</span>;
}

function RunningMeta({ e }: { e: Entry }) {
  if (e.status !== "running") return null;
  const parts: string[] = [];
  if (e.elapsedSeconds != null && e.elapsedSeconds > 0) {
    parts.push(`${e.elapsedSeconds.toFixed(1)}s`);
  }
  if (e.progressMessage) {
    parts.push(e.progressMessage);
  }
  if (!parts.length) return null;
  return (
    <span className="text-[10px] text-mc-faint animate-pulse ml-1">
      {parts.join(" · ")}
    </span>
  );
}

export function DiffBlock({ lines }: { lines: DiffLine[] }) {
  const [expanded, setExpanded] = useState(false);
  const { head, hidden, tail } = useMemo(() => collapseMiddle(lines), [lines]);
  const render = (ls: DiffLine[], keyBase: string) =>
    ls.map((l, i) => (
      <div
        key={`${keyBase}${i}`}
        className={
          "mc-diff-line " +
          (l.type === "add" ? "mc-diff-add" : l.type === "del" ? "mc-diff-del" : "")
        }
      >
        <span className="mc-diff-gutter">{l.lineNo ?? (l.type === "del" ? "-" : "")}</span>
        <span className={"mc-diff-text " + (l.type === "meta" ? "text-mc-faint" : "")}>
          {l.type === "add" ? "+ " : l.type === "del" ? "− " : "  "}
          {l.text}
        </span>
      </div>
    ));
  return (
    <div className="overflow-x-auto py-1">
      {render(head, "h")}
      {hidden.length > 0 && !expanded && (
        <button
          onClick={() => setExpanded(true)}
          className="w-full text-center text-[11px] text-mc-faint hover:text-mc-muted py-1 cursor-pointer border-y border-dashed border-mc-border"
        >
          ⋯ 展开中间 {hidden.length} 行 ⋯
        </button>
      )}
      {expanded && render(hidden, "m")}
      {render(tail, "t")}
    </div>
  );
}

// ── file_write / apply_patch → diff 卡 ─────────────────────────

function EditCard({ e }: { e: Entry }) {
  const [open, setOpen] = useState(true);
  const [showFull, setShowFull] = useState(false);
  const args = e.args || {};
  const path = str(args.path);
  const fileName = path.split(/[\\/]/).pop() || path;

  const { lines, stat, fullText } = useMemo((): {
    lines: DiffLine[];
    stat: DiffStat;
    fullText: string;
  } => {
    if (e.name === "apply_patch" && Array.isArray(args.edits)) {
      const all: DiffLine[] = [];
      let adds = 0;
      let dels = 0;
      for (const ed of args.edits as Array<Record<string, unknown>>) {
        const d = buildDiffFromStrings(str(ed.old_text), str(ed.new_text));
        all.push(...d.lines, { type: "meta", text: "", lineNo: null });
        adds += d.stat.adds;
        dels += d.stat.dels;
      }
      all.pop();
      const full = (args.edits as Array<Record<string, unknown>>)
        .map((ed) => str(ed.new_text))
        .join("\n⋯\n");
      return { lines: all, stat: { adds, dels }, fullText: full };
    }
    // file_write：全量新增
    const content = str(args.content);
    const d = buildDiffFromStrings("", content);
    return { lines: d.lines, stat: d.stat, fullText: content };
  }, [e.name, args]);

  const focusFile = useApp((s) => s.focusWorkspaceFile);
  return (
    <div className="border border-mc-border rounded-md bg-mc-panel2/60 min-w-0">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <StatusDot status={e.status} />
        <RunningMeta e={e} />
        <span className="text-xs text-mc-muted shrink-0">✎ 编辑</span>
        <button
          onClick={() => focusFile(path)}
          className="font-mono text-xs text-mc-text truncate cursor-pointer hover:text-mc-accent hover:underline decoration-mc-accent/50"
          title={`在右侧打开 ${path}`}
        >
          {fileName}
        </button>
        <button onClick={() => setOpen(!open)} className="flex items-center gap-2 cursor-pointer shrink-0">
          <span className="text-xs text-mc-ok">+{stat.adds}</span>
          <span className="text-xs text-mc-err">−{stat.dels}</span>
        </button>
        <div className="flex-1" />
        {open && (
          <button
            onClick={() => setShowFull(!showFull)}
            className={
              "text-[11px] px-2 py-0.5 rounded border cursor-pointer " +
              (showFull
                ? "border-mc-accent/50 text-mc-accent"
                : "border-mc-border text-mc-faint hover:text-mc-muted")
            }
          >
            预览
          </button>
        )}
        <button onClick={() => setOpen(!open)} className="text-mc-faint text-xs cursor-pointer">
          {open ? "▾" : "▸"}
        </button>
      </div>
      {open && !showFull && <DiffBlock lines={lines} />}
      {open && showFull && (
        <pre className="px-3 py-2 text-[12px] font-mono text-mc-text whitespace-pre-wrap break-all max-h-96 overflow-y-auto border-t border-mc-border">
          {fullText.slice(0, 20000)}
        </pre>
      )}
      {e.status === "error" && e.result && (
        <div className="px-3 pb-2 text-[11px] text-mc-err font-mono">{String(e.result).slice(0, 300)}</div>
      )}
    </div>
  );
}

// ── bash → 终端卡 ───────────────────────────────────────────────

function TerminalCard({ e }: { e: Entry }) {
  const [open, setOpen] = useState(false);
  const cmd = str(e.args?.command) || str(e.args?.code);
  const out = e.result != null ? String(e.result) : "";
  const tail = out.length > 2400 && !open ? out.slice(-2400) : out;
  return (
    <div className="border border-mc-border rounded-md bg-black/40 min-w-0">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left cursor-pointer"
      >
        <StatusDot status={e.status} />
        <RunningMeta e={e} />
        <span className="font-mono text-xs text-mc-warn shrink-0">$</span>
        <span className="font-mono text-xs text-mc-text truncate flex-1">{cmd}</span>
        <span className="text-mc-faint text-xs">{open ? "▾" : "▸"}</span>
      </button>
      {(open || (e.status !== "running" && out)) && (
        <pre
          className={
            "px-3 pb-2 text-[11.5px] font-mono text-mc-muted whitespace-pre-wrap break-all overflow-y-auto " +
            (open ? "max-h-96" : "max-h-40")
          }
        >
          {out.length > tail.length && <span className="text-mc-faint">⋯（截断，点头部展开）\n</span>}
          {tail.slice(0, 20000)}
        </pre>
      )}
    </div>
  );
}

// ── file_read / glob / grep / list_dir → 单行摘要卡 ────────────

const SUMMARY_TOOLS: Record<string, string> = {
  file_read: "📄 读取",
  glob_files: "🔍 找文件",
  grep_files: "🔍 搜索",
  list_dir: "📁 列目录",
  web_search: "🌐 搜索",
  web_fetch: "🌐 抓取",
  memory_search: "🧠 检索记忆",
  remember: "🧠 写入记忆",
  // ``think`` 不在此 — 它在 ToolCard 路由里特判成「思考过程」折叠块（图二）。
};

function SummaryCard({ e }: { e: Entry }) {
  const [open, setOpen] = useState(false);
  const focusFile = useApp((s) => s.focusWorkspaceFile);
  const label = SUMMARY_TOOLS[e.name || ""] || `⚙ ${e.name}`;
  const path = str(e.args?.path);
  const arg =
    path || str(e.args?.pattern) || str(e.args?.query) || str(e.args?.url) ||
    str(e.args?.text)?.slice(0, 80) ||
    "";
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-2 max-w-full">
        <StatusDot status={e.status} />
        <RunningMeta e={e} />
        <span className="text-xs text-mc-muted shrink-0">{label}</span>
        {path ? (
          <button
            onClick={() => focusFile(path)}
            className="font-mono text-[11.5px] text-mc-faint truncate cursor-pointer hover:text-mc-accent hover:underline decoration-mc-accent/50"
            title={`在右侧打开 ${path}`}
          >
            {arg}
          </button>
        ) : (
          <span className="font-mono text-[11.5px] text-mc-faint truncate">{arg}</span>
        )}
        <button
          onClick={() => setOpen(!open)}
          className="text-mc-faint text-[10px] cursor-pointer px-1"
          aria-label="展开结果"
        >
          {open ? "▾" : "▸"}
        </button>
      </div>
      {open && e.result != null && (
        <pre className="mt-1 px-3 py-2 text-[11px] font-mono text-mc-muted whitespace-pre-wrap break-all max-h-64 overflow-y-auto border border-mc-border rounded-md bg-mc-panel2/60">
          {String(e.result).slice(0, 8000)}
        </pre>
      )}
    </div>
  );
}

// ── 通用卡（兜底） + 截图缩略 ──────────────────────────────────

function GenericCard({ e }: { e: Entry }) {
  const [open, setOpen] = useState(false);
  const argsSummary = useMemo(() => {
    try {
      const s = JSON.stringify(e.args || {});
      return s.length > 110 ? s.slice(0, 110) + "…" : s;
    } catch {
      return "";
    }
  }, [e.args]);
  return (
    <div className="border border-mc-border rounded-md bg-mc-panel2/60 min-w-0">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left cursor-pointer"
      >
        <StatusDot status={e.status} />
        <RunningMeta e={e} />
        <span className="font-mono text-xs text-mc-text shrink-0">{e.name}</span>
        <span className="font-mono text-[11px] text-mc-faint truncate flex-1">{argsSummary}</span>
        <span className="text-mc-faint text-xs">{open ? "▾" : "▸"}</span>
      </button>
      {open && e.result != null && (
        <pre className="px-3 pb-2 text-[11px] font-mono text-mc-muted whitespace-pre-wrap break-all max-h-64 overflow-y-auto">
          {String(e.result).slice(0, 8000)}
        </pre>
      )}
    </div>
  );
}

function BrokenMedia({ name }: { name: string }) {
  return (
    <div className="h-28 w-36 rounded border border-dashed border-mc-border flex flex-col items-center justify-center text-mc-faint gap-1">
      <span className="text-lg">🖼</span>
      <span className="text-[10px] px-2 text-center break-all">{name} 加载失败</span>
    </div>
  );
}

function MediaStrip({ e }: { e: Entry }) {
  const openLightbox = useApp((s) => s.openLightbox);
  const [broken, setBroken] = useState<Record<string, boolean>>({});
  if (!e.images?.length && !e.videos?.length && !e.audios?.length && !e.documents?.length)
    return null;
  return (
    <div className="flex gap-2 flex-wrap mt-1.5 items-start">
      {(e.images || []).map((src) =>
        broken[src] ? (
          <BrokenMedia key={src} name={src.split("/").pop()?.split("?")[0] || "image"} />
        ) : (
          <button key={src} onClick={() => openLightbox(src, "image")} className="cursor-zoom-in">
            <img
              src={src}
              className="h-28 rounded border border-mc-border hover:border-mc-accent/60"
              alt="tool media"
              onError={() => setBroken((b) => ({ ...b, [src]: true }))}
            />
          </button>
        ),
      )}
      {(e.videos || []).map((src) => (
        <video
          key={src}
          src={src}
          controls
          preload="metadata"
          className="h-40 max-w-72 rounded border border-mc-border cursor-zoom-in"
          onClick={(ev) => {
            // 点画面区放大；点控制条正常操作。
            const v = ev.currentTarget;
            if (ev.clientY < v.getBoundingClientRect().bottom - 40) {
              ev.preventDefault();
              openLightbox(src, "video");
            }
          }}
        />
      ))}
      {(e.audios || []).map((src) => (
        <audio key={src} src={src} controls preload="metadata" className="h-9" />
      ))}
      {(e.documents || []).map((d) => (
        <a
          key={d.url}
          href={d.url}
          download={d.name}
          target="_blank"
          rel="noreferrer"
          className="flex items-center gap-2 px-3 py-2 rounded-md border border-mc-border bg-mc-panel2 hover:border-mc-accent/50 cursor-pointer max-w-xs"
          title={`下载 ${d.name}`}
        >
          <span className="text-lg shrink-0">{docIcon(d.mime, d.name)}</span>
          <span className="text-[12px] truncate flex-1">{d.name}</span>
          <span className="text-[10px] text-mc-faint shrink-0">↓</span>
        </a>
      ))}
    </div>
  );
}

function docIcon(mime?: string, name?: string): string {
  const ext = (name || "").split(".").pop()?.toLowerCase() || "";
  if (mime?.includes("sheet") || ext === "xlsx" || ext === "xls" || ext === "csv") return "📊";
  if (mime?.includes("word") || ext === "docx" || ext === "doc") return "📝";
  if (mime?.includes("presentation") || ext === "pptx") return "📑";
  if (mime === "application/pdf" || ext === "pdf") return "📕";
  if (mime?.includes("zip") || ext === "zip") return "🗜";
  return "📄";
}

// ── fanout：组长任务拆解 + 角色/任务分配总览 ───────────────────
// 对位「派专家团」：fanout_started 事件带 goal/synthesis/plan[]，
// plan 的每一项是 { index, role, subtask, specialist } —— 即拆出的
// 子任务 + 指派的专家角色。之前 Timeline 没有 fanout 分支，这条只
// 当普通文字泡渲染，拆解与分配完全不可见（用户报「流程未直观展示」）。

const _ROLES = ["general", "code", "research", "ops", "comm"] as const;

type PlanItem = { index: number; role: string; subtask: string; specialist: string };

function FanoutReviewEditor({ e }: { e: Entry }) {
  // 派发前编辑拆解：本地可编辑副本，确认后回传 submitFanoutReview。
  const submit = useApp((s) => s.submitFanoutReview);
  const [items, setItems] = useState<PlanItem[]>(() =>
    (e.plan || []).map((p) => ({ ...p })),
  );
  const [synthesis, setSynthesis] = useState(e.synthesis || "concat");
  const reviewId = e.reviewId || "";

  const update = (i: number, patch: Partial<PlanItem>) =>
    setItems((arr) => arr.map((it, j) => (j === i ? { ...it, ...patch } : it)));
  const remove = (i: number) => setItems((arr) => arr.filter((_, j) => j !== i));
  const add = () =>
    setItems((arr) => [...arr, { index: arr.length, role: "general", subtask: "", specialist: "" }]);

  const cleaned = items
    .map((it) => ({ ...it, subtask: it.subtask.trim() }))
    .filter((it) => it.subtask)
    .map((it, i) => ({ ...it, index: i }));
  const valid = cleaned.length >= 2 && cleaned.length <= 8;

  return (
    <div className="border border-mc-warn/50 bg-mc-warn/5 rounded-md min-w-0">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <span className="text-sm">👥</span>
        <span className="text-xs font-medium text-mc-warn">专家团 · 派发前确认拆解</span>
        <span className="text-[11px] text-mc-faint shrink-0">{cleaned.length} 个子任务</span>
      </div>
      <div className="px-3 pb-2.5 space-y-2">
        {e.goal && (
          <div className="text-[11.5px] text-mc-muted">
            <span className="text-mc-faint">目标：</span>
            {e.goal}
          </div>
        )}
        <div className="space-y-1.5">
          {items.map((it, i) => (
            <div key={i} className="flex gap-1.5 items-start">
              <span className="text-[10px] font-mono text-mc-faint mt-2 shrink-0 w-5">#{i}</span>
              <select
                value={it.role}
                onChange={(ev) => update(i, { role: ev.target.value })}
                className="text-[11px] bg-mc-panel border border-mc-border rounded px-1 py-1 mt-0.5 text-mc-accent outline-none focus:border-mc-accent cursor-pointer"
              >
                {_ROLES.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
              <textarea
                value={it.subtask}
                onChange={(ev) => update(i, { subtask: ev.target.value })}
                rows={2}
                placeholder="子任务描述…"
                className="flex-1 text-[11.5px] bg-mc-panel border border-mc-border rounded px-2 py-1 text-mc-text outline-none focus:border-mc-accent resize-y min-w-0"
              />
              <button
                onClick={() => remove(i)}
                className="text-mc-faint hover:text-mc-err text-xs mt-1.5 cursor-pointer px-1"
                title="删除子任务"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <button
            onClick={add}
            disabled={items.length >= 8}
            className="text-[11px] px-2 py-1 rounded border border-mc-border text-mc-muted hover:border-mc-accent/60 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
          >
            + 加子任务
          </button>
          <span className="text-[11px] text-mc-faint">汇总：</span>
          <select
            value={synthesis}
            onChange={(ev) => setSynthesis(ev.target.value)}
            className="text-[11px] bg-mc-panel border border-mc-border rounded px-1 py-1 text-mc-muted outline-none focus:border-mc-accent cursor-pointer"
          >
            <option value="concat">拼接</option>
            <option value="llm">LLM 归纳</option>
          </select>
          <div className="flex-1" />
          <button
            onClick={() => submit(reviewId, [], synthesis, true)}
            className="text-[11px] px-3 py-1 rounded border border-mc-border text-mc-faint hover:text-mc-err cursor-pointer"
          >
            取消
          </button>
          <button
            onClick={() => valid && submit(reviewId, cleaned, synthesis, false)}
            disabled={!valid}
            className="text-[11px] px-3 py-1 rounded bg-mc-accent/20 border border-mc-accent/50 text-mc-accent hover:bg-mc-accent/30 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
            title={valid ? "确认并派发" : "子任务需 2-8 个且非空"}
          >
            确认派发
          </button>
        </div>
      </div>
    </div>
  );
}

export function FanoutCard({ e }: { e: Entry }) {
  const [open, setOpen] = useState(true);
  // 派发前编辑态：渲染可编辑的拆解方案，等用户确认。
  if (e.status === "review") return <FanoutReviewEditor e={e} />;
  const plan = e.plan || [];
  const total = e.total || plan.length;
  return (
    <div className="border border-mc-accent/40 bg-mc-accent/5 rounded-md min-w-0">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left cursor-pointer"
      >
        <span className="text-sm">👥</span>
        <span className="text-xs font-medium text-mc-text">专家团 · 任务拆解</span>
        <span className="text-[11px] text-mc-accent shrink-0">{total} 个子任务</span>
        {e.status === "cancelled" && (
          <span className="text-[11px] text-mc-faint shrink-0">· 已取消</span>
        )}
        <div className="flex-1" />
        <span className="text-mc-faint text-xs">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="px-3 pb-2.5 space-y-2">
          {e.goal && (
            <div className="text-[11.5px] text-mc-muted">
              <span className="text-mc-faint">目标：</span>
              {e.goal}
            </div>
          )}
          {plan.length > 0 ? (
            <div className="space-y-1.5">
              {plan.map((p) => (
                <div
                  key={p.index}
                  className="flex gap-2 items-start border-l-2 border-mc-accent/40 pl-2.5 py-0.5"
                >
                  <span className="text-[10px] font-mono text-mc-faint mt-0.5 shrink-0">#{p.index}</span>
                  <div className="min-w-0 flex-1">
                    <span className="inline-block text-[10px] px-1.5 py-0.5 rounded bg-mc-accent/15 text-mc-accent mr-1.5">
                      {p.role || p.specialist || "general"}
                    </span>
                    <span className="text-[11.5px] text-mc-text whitespace-pre-wrap">{p.subtask}</span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-[11.5px] text-mc-faint">{e.content || "组长正在拆解任务…"}</div>
          )}
          {e.synthesis && (
            <div className="text-[11px] text-mc-faint border-t border-mc-border/60 pt-1.5">
              汇总策略：{e.synthesis}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── worker / subagent 执行组（对位 Claude Code 的 Agent 折叠组） ──

export function AgentGroupCard({ e }: { e: Entry }) {
  const [open, setOpen] = useState(e.status === "running");
  const title =
    e.kind === "worker"
      ? `Worker ${e.workerId} · 任务 ${e.taskId}`
      : `专家 · ${e.roleHint || "general"} #${e.subagentIndex}`;
  return (
    <div
      className={
        "border rounded-md min-w-0 " +
        (e.status === "running" ? "border-mc-accent/40 bg-mc-accent/5" : "border-mc-border bg-mc-panel2/60")
      }
    >
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left cursor-pointer"
      >
        <span className="text-sm">🤖</span>
        <span className="text-xs font-medium text-mc-text truncate">{title}</span>
        {e.status === "running" ? (
          <span className="text-[11px] text-mc-accent animate-pulse shrink-0">执行中…</span>
        ) : (
          <span className={"text-[11px] shrink-0 " + (e.status === "error" ? "text-mc-err" : "text-mc-ok")}>
            {e.status === "error" ? "失败" : "完成"}
            {e.hops ? ` · ${e.hops} hops` : ""}
            {e.elapsedSeconds != null ? ` · ${Math.round(Number(e.elapsedSeconds))}s` : ""}
          </span>
        )}
        <div className="flex-1" />
        <span className="text-mc-faint text-xs">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="px-3 pb-2 space-y-1.5">
          {e.promptPreview && (
            <div className="text-[11.5px] text-mc-faint border-l-2 border-mc-border pl-2 whitespace-pre-wrap">
              <span className="text-mc-muted">分配任务：</span>
              {e.promptPreview}
            </div>
          )}
          {e.outputPreview && (
            <pre className="text-[11.5px] font-mono text-mc-muted whitespace-pre-wrap break-all max-h-64 overflow-y-auto">
              {e.outputPreview}
            </pre>
          )}
          {e.errorPreview && <div className="text-[11.5px] text-mc-err">{e.errorPreview}</div>}
        </div>
      )}
    </div>
  );
}

// ── 路由器 ─────────────────────────────────────────────────────

export default function ToolCard({ e }: { e: Entry }) {
  const name = e.name || "";
  // 用户要求：``think`` 工具走「思考过程」折叠块（图二），不是普通工具卡（图一）。
  // 内容是它的 args.thought（深思模式记录的可审计推理）。
  if (name === "think") {
    return (
      <div className="min-w-0">
        <ThinkingBlock content={str(e.args?.thought) || str(e.content) || ""} />
        <MediaStrip e={e} />
      </div>
    );
  }
  let card;
  if (name === "file_write" || name === "apply_patch") card = <EditCard e={e} />;
  else if (name === "bash") card = <TerminalCard e={e} />;
  else if (name in SUMMARY_TOOLS) card = <SummaryCard e={e} />;
  else card = <GenericCard e={e} />;
  return (
    <div className="min-w-0">
      {card}
      <MediaStrip e={e} />
    </div>
  );
}
