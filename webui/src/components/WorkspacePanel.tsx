// 右栏常驻工作区（10.M2 实时预览深度融合）。
//
//   Diff  — session_workspaces commits/diff API，workspace_file_changed
//           事件驱动自动刷新 + 角标
//   文件  — 树 + 点击查看（md 渲染 / 代码高亮原文 / 图片走 /raw）；
//           时间线 diff 卡"在工作区查看"联动聚焦
//   预览  — agent 实时视觉流：canvas artifact（html/svg 沙箱渲染）
//           + 工具截图流（computer-use/browser）实时滚入
//   终端  — bash 工具输出聚合流（最近 N 条）
//
// 跟随模式（默认开）：新 artifact/截图 → 自动切预览；文件变更 → Diff 角标。

import { useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../store/app";
import { apiGet } from "../lib/api";
import Markdown from "./LazyMarkdown";
import MermaidView from "./MermaidView";
import { parseUnifiedDiff } from "../lib/difflines";
import { DiffBlock } from "./ToolCards";

const TABS = ["预览", "Diff", "文件", "终端"] as const;
type Tab = (typeof TABS)[number];

interface TreeNode {
  path: string;
  size?: number;
}
interface Commit {
  sha: string;
  message?: string;
  ts?: number;
}

const MD_EXTS = new Set(["md", "markdown", "mdx"]);
const IMG_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "svg"]);
const ext = (p: string) => (p.split(".").pop() || "").toLowerCase();

// ── 预览 tab ───────────────────────────────────────────────────

function ArtifactView({ kind, content, title }: { kind: string; content: string; title: string }) {
  // mermaid 真正渲染成图（懒加载 mermaid），不再塞裸源码进 iframe。
  if (kind === "mermaid") {
    return <MermaidView content={content} />;
  }
  if (kind === "html" || kind === "svg") {
    // 沙箱 iframe（allow-scripts，无 same-origin —— 与 Phase 9 桥同纪律）。
    const doc =
      kind === "svg"
        ? `<body style="margin:0;background:#fff">${content}</body>`
        : content;
    return (
      <iframe
        sandbox="allow-scripts"
        srcDoc={doc}
        title={title}
        className="w-full h-72 rounded border border-mc-border bg-white"
      />
    );
  }
  // table / 其它结构化文本 → markdown 渲染（表格、列表、代码块都比裸 <pre> 好看）。
  if (kind === "table" || kind === "markdown" || kind === "md") {
    return (
      <div className="max-h-[28rem] overflow-auto rounded border border-mc-border p-2 text-sm">
        <Markdown text={content.slice(0, 12000)} />
      </div>
    );
  }
  return (
    <pre className="text-[11px] font-mono text-mc-muted whitespace-pre-wrap break-all max-h-72 overflow-y-auto border border-mc-border rounded p-2">
      {content.slice(0, 8000)}
    </pre>
  );
}

function LiveShotItem({ url, tool, latest }: { url: string; tool: string; latest: boolean }) {
  const openLightbox = useApp((s) => s.openLightbox);
  const [broken, setBroken] = useState(false);
  if (broken) {
    return (
      <div className="w-full rounded border border-dashed border-mc-border py-3 text-center text-[10px] text-mc-faint">
        {tool} · 图像加载失败（{url.split("/").pop()?.split("?")[0]}）
      </div>
    );
  }
  return (
    <button onClick={() => openLightbox(url, "image")} className="block w-full cursor-zoom-in text-left">
      <img
        src={url}
        className={"w-full rounded border " + (latest ? "border-mc-accent/60" : "border-mc-border opacity-80")}
        alt={tool}
        onError={() => setBroken(true)}
      />
      <div className="text-[10px] text-mc-faint mt-0.5">{tool}</div>
    </button>
  );
}

function PreviewTab() {
  const artifacts = useApp((s) => s.chat.artifacts);
  const liveShots = useApp((s) => s.chat.liveShots);
  if (artifacts.length === 0 && liveShots.length === 0) {
    return (
      <div className="text-xs text-mc-faint p-3">
        agent 的实时画面会出现在这里 — canvas 产物、computer-use /浏览器截图都会实时滚入
      </div>
    );
  }
  return (
    <div className="p-3 space-y-3">
      {artifacts.map((a) => (
        <div key={a.id}>
          <div className="text-xs text-mc-muted mb-1 flex items-center gap-2">
            <span className="text-mc-accent">◈</span>
            <span className="truncate">{a.title}</span>
            <span className="text-mc-faint">{a.kind}</span>
          </div>
          <ArtifactView kind={a.kind} content={a.content} title={a.title} />
        </div>
      ))}
      {liveShots.length > 0 && (
        <div>
          <div className="text-xs text-mc-muted mb-1.5">
            <span className="text-mc-accent animate-pulse">●</span> 视觉流（最近 {liveShots.length} 帧）
          </div>
          <div className="space-y-2">
            {liveShots.map((s, i) => (
              <LiveShotItem key={`${s.url}_${i}`} url={s.url} tool={s.tool} latest={i === 0} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Diff tab ───────────────────────────────────────────────────

function DiffTab() {
  const token = useApp((s) => s.token);
  const sid = useApp((s) => s.sid);
  const version = useApp((s) => s.chat.workspaceVersion);
  const [commits, setCommits] = useState<Commit[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [diffText, setDiffText] = useState("");

  useEffect(() => {
    if (!token || !sid) return;
    apiGet<{ commits?: Commit[] }>(`/api/v2/session_workspaces/${encodeURIComponent(sid)}/commits`, token)
      .then((d) => {
        const list = d?.commits || [];
        setCommits(list);
        // 新变更进来时自动选中最新 commit（实时感）。
        if (list.length > 0) setSel((cur) => cur ?? list[0].sha);
      })
      .catch(() => setCommits([]));
  }, [token, sid, version]);

  useEffect(() => {
    if (!token || !sid || !sel) return;
    apiGet<{ diff?: string }>(
      `/api/v2/session_workspaces/${encodeURIComponent(sid)}/diff?commit=${encodeURIComponent(sel)}`,
      token,
    )
      .then((d) => setDiffText(d?.diff || ""))
      .catch(() => setDiffText(""));
  }, [token, sid, sel]);

  const parsed = useMemo(() => (diffText ? parseUnifiedDiff(diffText) : null), [diffText]);

  if (commits.length === 0)
    return <div className="text-xs text-mc-faint p-3">还没有改动 — agent 写文件后这里出现 diff 时间线</div>;
  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="shrink-0 max-h-32 overflow-y-auto border-b border-mc-border">
        {commits.map((c) => (
          <button
            key={c.sha}
            onClick={() => setSel(c.sha)}
            className={
              "w-full text-left px-3 py-1.5 text-[11.5px] cursor-pointer truncate " +
              (sel === c.sha ? "bg-mc-accent/10 text-mc-text" : "text-mc-muted hover:bg-mc-panel2")
            }
          >
            <span className="font-mono text-mc-faint">{c.sha.slice(0, 7)}</span> {c.message || ""}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-y-auto">
        {parsed ? (
          <>
            <div className="px-3 pt-2 text-[11px] text-mc-faint">
              <span className="text-mc-ok">+{parsed.stat.adds}</span>{" "}
              <span className="text-mc-err">−{parsed.stat.dels}</span>
            </div>
            <DiffBlock lines={parsed.lines.filter((l) => l.type !== "meta" || l.text.startsWith("+++"))} />
          </>
        ) : (
          <div className="text-xs text-mc-faint p-3">选择一个改动查看 diff</div>
        )}
      </div>
    </div>
  );
}

// ── 文件 tab ───────────────────────────────────────────────────

function FilesTab() {
  const token = useApp((s) => s.token);
  const sid = useApp((s) => s.sid);
  const version = useApp((s) => s.chat.workspaceVersion);
  const focus = useApp((s) => s.workspaceFocus);
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [selPath, setSelPath] = useState<string | null>(null);
  const [file, setFile] = useState<{ content?: string; kind?: string } | null>(null);

  useEffect(() => {
    if (!token || !sid) return;
    apiGet<{ tree?: TreeNode[]; files?: TreeNode[] }>(
      `/api/v2/session_workspaces/${encodeURIComponent(sid)}/tree`,
      token,
    )
      .then((d) => setTree(d?.tree || d?.files || []))
      .catch(() => setTree([]));
  }, [token, sid, version]);

  // 时间线点击文件名联动：绝对路径里截出 workspace 相对段尽力匹配；
  // 不在工作区的文件给出明确反馈而不是没反应。
  const [focusMiss, setFocusMiss] = useState<string | null>(null);
  useEffect(() => {
    if (!focus) return;
    const base = focus.path.split(/[\\/]/).pop() || focus.path;
    const hit = tree.find((n) => n.path === focus.path || n.path.endsWith(base));
    if (hit) {
      setSelPath(hit.path);
      setFocusMiss(null);
    } else {
      setFocusMiss(focus.path);
    }
  }, [focus, tree]);

  useEffect(() => {
    if (!token || !sid || !selPath) return;
    apiGet<{ content?: string; kind?: string }>(
      `/api/v2/session_workspaces/${encodeURIComponent(sid)}/file?path=${encodeURIComponent(selPath)}`,
      token,
    )
      .then(setFile)
      .catch(() => setFile(null));
  }, [token, sid, selPath]);

  const missNotice = focusMiss && (
    <div className="mx-2 mt-2 px-2.5 py-1.5 rounded border border-mc-warn/40 bg-mc-warn/5 text-[11px] text-mc-warn break-all">
      该文件不在本会话工作区（{focusMiss}）— 工作区只索引 agent 在 scratch 目录的产物
    </div>
  );

  if (tree.length === 0)
    return (
      <div>
        {missNotice}
        <div className="text-xs text-mc-faint p-3">工作区暂无文件 — agent 产出后实时出现</div>
      </div>
    );

  if (selPath) {
    const e = ext(selPath);
    const rawUrl = `/api/v2/session_workspaces/${encodeURIComponent(sid)}/raw?path=${encodeURIComponent(selPath)}${token ? `&token=${encodeURIComponent(token)}` : ""}`;
    return (
      <div className="flex flex-col h-full min-h-0">
        <button
          onClick={() => setSelPath(null)}
          className="shrink-0 text-left px-3 py-1.5 text-[11.5px] text-mc-muted hover:text-mc-text cursor-pointer border-b border-mc-border font-mono truncate"
        >
          ← {selPath}
        </button>
        <div className="flex-1 overflow-y-auto p-3">
          {IMG_EXTS.has(e) ? (
            <img src={rawUrl} className="max-w-full rounded border border-mc-border" alt={selPath} />
          ) : MD_EXTS.has(e) ? (
            <Markdown text={file?.content || ""} />
          ) : file?.kind === "binary" ? (
            <div className="text-xs text-mc-faint">二进制文件</div>
          ) : (
            <pre className="text-[11.5px] font-mono text-mc-text whitespace-pre-wrap break-all">
              {(file?.content || "").slice(0, 40000)}
            </pre>
          )}
        </div>
      </div>
    );
  }

  return (
    <ul className="p-2 space-y-0.5">
      {missNotice && <li>{missNotice}</li>}
      {tree.map((n) => (
        <li key={n.path}>
          <button
            onClick={() => setSelPath(n.path)}
            className="w-full text-left text-xs font-mono text-mc-muted hover:text-mc-text hover:bg-mc-panel2 rounded px-1.5 py-0.5 cursor-pointer truncate"
            title={n.path}
          >
            {n.path}
          </button>
        </li>
      ))}
    </ul>
  );
}

// ── 终端 tab（bash 工具输出聚合流） ────────────────────────────

function TerminalTab() {
  const entries = useApp((s) => s.chat.entries);
  const runs = useMemo(
    () => entries.filter((e) => e.kind === "tool_use" && e.name === "bash").slice(-20),
    [entries],
  );
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView();
  }, [runs.length]);
  if (runs.length === 0)
    return <div className="text-xs text-mc-faint p-3">agent 跑过的命令和输出会聚合在这里</div>;
  return (
    <div className="p-2 font-mono text-[11.5px] space-y-2 bg-black/30 min-h-full">
      {runs.map((r) => (
        <div key={r.id}>
          <div className="text-mc-warn">
            $ {String(r.args?.command || "")}
            {r.status === "running" && <span className="text-mc-accent animate-pulse"> ●</span>}
          </div>
          {r.result != null && (
            <pre className="text-mc-muted whitespace-pre-wrap break-all max-h-48 overflow-y-auto">
              {String(r.result).slice(-3000)}
            </pre>
          )}
        </div>
      ))}
      <div ref={endRef} />
    </div>
  );
}

// ── 面板壳 + 跟随逻辑 ──────────────────────────────────────────

export default function WorkspacePanel({ width }: { width?: number }) {
  const [tab, setTab] = useState<Tab>("预览");
  const follow = useApp((s) => s.followAgent);
  const setFollow = useApp((s) => s.setFollowAgent);
  const artifacts = useApp((s) => s.chat.artifacts);
  const liveShots = useApp((s) => s.chat.liveShots);
  const version = useApp((s) => s.chat.workspaceVersion);
  const focus = useApp((s) => s.workspaceFocus);
  const [diffBadge, setDiffBadge] = useState(false);

  // 跟随 agent：新视觉信号 → 自动切预览；文件变更 → Diff 角标（不抢屏）。
  const prevShots = useRef(0);
  const prevArts = useRef(0);
  useEffect(() => {
    if (follow && (liveShots.length > prevShots.current || artifacts.length > prevArts.current)) {
      setTab("预览");
    }
    prevShots.current = liveShots.length;
    prevArts.current = artifacts.length;
  }, [liveShots.length, artifacts.length, follow]);

  const prevVersion = useRef(0);
  useEffect(() => {
    if (version > prevVersion.current && tab !== "Diff") setDiffBadge(true);
    prevVersion.current = version;
  }, [version, tab]);

  // 时间线"在工作区查看" → 切到文件 tab。
  useEffect(() => {
    if (focus) setTab("文件");
  }, [focus]);

  return (
    <aside
      style={width ? { width } : undefined}
      className="w-80 border-l border-mc-border bg-mc-panel hidden xl:flex flex-col shrink-0"
    >
      <div className="flex items-center gap-1 px-2 pt-2 border-b border-mc-border shrink-0">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => {
              setTab(t);
              if (t === "Diff") setDiffBadge(false);
            }}
            className={
              "text-xs px-3 py-1.5 rounded-t-md cursor-pointer relative " +
              (tab === t ? "bg-mc-panel2 text-mc-text font-medium" : "text-mc-faint hover:text-mc-muted")
            }
          >
            {t}
            {t === "Diff" && diffBadge && (
              <span className="absolute top-1 right-1 w-1.5 h-1.5 rounded-full bg-mc-warn" />
            )}
          </button>
        ))}
        <div className="flex-1" />
        <button
          onClick={() => setFollow(!follow)}
          title="新产物/截图自动切到预览"
          className={
            "text-[10px] px-2 py-0.5 rounded-full border cursor-pointer mb-1 " +
            (follow ? "border-mc-accent/50 text-mc-accent" : "border-mc-border text-mc-faint")
          }
        >
          {follow ? "● 跟随" : "○ 跟随"}
        </button>
      </div>
      <div className="flex-1 overflow-y-auto min-h-0">
        {tab === "预览" && <PreviewTab />}
        {tab === "Diff" && <DiffTab />}
        {tab === "文件" && <FilesTab />}
        {tab === "终端" && <TerminalTab />}
      </div>
    </aside>
  );
}
