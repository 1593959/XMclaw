// 文件域：工作区 markdown 文件树 + 编辑器（2026-06-17 用户点名「在前端加上
// md 文件树，我自己修改」；同日美化 UI）。读/写走 /api/v2/files。
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useApp } from "../store/app";
import { apiGetFresh, apiSend } from "../lib/api";

const Markdown = lazy(() => import("../components/LazyMarkdown"));

interface Entry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
}
interface Root {
  key: string;
  label: string;
  path: string;
  exists: boolean;
}

const ROOT_ICON: Record<string, string> = {
  personas: "🎭",
  memory: "🧠",
  skills: "⚡",
  agents: "🤖",
  workspaces: "🗂",
};

const TEXT_RE = /\.(md|markdown|txt|json|ya?ml|toml|csv|py|js|ts|tsx|css|html|env|ini|cfg)$/i;
const isText = (n: string) => TEXT_RE.test(n);
const isMd = (n: string) => /\.(md|markdown)$/i.test(n);

function fileIcon(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() || "";
  if (ext === "md" || ext === "markdown") return "📝";
  if (ext === "json") return "🔧";
  if (ext === "yaml" || ext === "yml" || ext === "toml" || ext === "ini" || ext === "cfg") return "⚙️";
  if (ext === "py") return "🐍";
  if (["js", "ts", "tsx", "css", "html"].includes(ext)) return "📜";
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext)) return "🖼️";
  return "📄";
}

function fmtSize(n: number | null): string {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export default function FilesView() {
  const token = useApp((s) => s.token);
  const showToast = useApp((s) => s.showToast);

  const [roots, setRoots] = useState<Root[]>([]);
  const [children, setChildren] = useState<Record<string, Entry[]>>({});
  const [loading, setLoading] = useState<Record<string, boolean>>({});

  const [sel, setSel] = useState<Entry | null>(null);
  const [content, setContent] = useState("");
  const [original, setOriginal] = useState("");
  const [readOnly, setReadOnly] = useState(false);
  const [saving, setSaving] = useState(false);
  const [fileErr, setFileErr] = useState<string | null>(null);
  const [mode, setMode] = useState<"edit" | "preview">("edit");
  const openSeq = useRef(0);

  useEffect(() => {
    if (!token) return;
    const ctl = new AbortController();
    apiGetFresh<{ roots: Root[] }>("/api/v2/files/roots", token, ctl.signal)
      .then((d) => setRoots(d.roots || []))
      .catch(() => {
        if (!ctl.signal.aborted) setRoots([]);
      });
    return () => ctl.abort();
  }, [token]);

  async function loadDir(path: string) {
    if (!token) return;
    if (children[path]) {
      setChildren((c) => {
        const n = { ...c };
        delete n[path];
        return n;
      });
      return;
    }
    setLoading((l) => ({ ...l, [path]: true }));
    try {
      const d = await apiGetFresh<{ entries?: Entry[] }>(
        `/api/v2/files?path=${encodeURIComponent(path)}`, token);
      setChildren((c) => ({ ...c, [path]: d.entries || [] }));
    } catch {
      showToast("目录读取失败", "err");
    } finally {
      setLoading((l) => ({ ...l, [path]: false }));
    }
  }

  async function openFile(e: Entry) {
    if (!token) return;
    const seq = openSeq.current + 1;
    openSeq.current = seq;
    setSel(e);
    setFileErr(null);
    setReadOnly(!isText(e.name));
    setMode(isMd(e.name) ? "preview" : "edit");
    try {
      const d = await apiGetFresh<{ content?: string }>(
        `/api/v2/files?path=${encodeURIComponent(e.path)}`, token);
      if (openSeq.current !== seq) return;
      setContent(d.content ?? "");
      setOriginal(d.content ?? "");
    } catch {
      if (openSeq.current !== seq) return;
      setFileErr("文件读取失败（可能超过 1 MiB 或非文本）");
      setContent("");
      setOriginal("");
    }
  }

  async function save() {
    if (!token || sel == null) return;
    setSaving(true);
    try {
      await apiSend("PUT", "/api/v2/files", { path: sel.path, content }, token);
      setOriginal(content);
      showToast("已保存", "ok");
    } catch (err) {
      const msg = (err as Error)?.message || "";
      showToast(msg.includes("403") ? "该文件不在可写根内，拒绝写入" : "保存失败", "err");
    } finally {
      setSaving(false);
    }
  }

  const dirty = content !== original;

  return (
    <div className="flex-1 flex min-h-0 mc-rise">
      {/* 左：文件树 */}
      <aside className="w-72 shrink-0 border-r border-mc-border flex flex-col bg-mc-panel/40">
        <div className="shrink-0 px-3 py-2.5 border-b border-mc-border flex items-center gap-2">
          <span className="text-base leading-none">🗂</span>
          <div>
            <div className="text-sm text-mc-text leading-tight">文件域</div>
            <div className="text-[10px] text-mc-faint leading-tight">工作区 · 可编辑 md</div>
          </div>
        </div>
        <div className="flex-1 overflow-auto py-1.5 text-sm">
          {roots.length === 0 && (
            <div className="px-3 py-2 text-mc-faint text-xs">加载中…</div>
          )}
          {roots.map((r) => (
            <TreeNode
              key={r.path}
              name={`${ROOT_ICON[r.key] || "📁"}  ${r.label}`}
              path={r.path}
              isDir
              depth={0}
              isRoot
              dim={!r.exists}
              children_={children}
              loading={loading}
              selected={sel?.path ?? null}
              onToggleDir={loadDir}
              onOpenFile={openFile}
            />
          ))}
        </div>
      </aside>

      {/* 右：编辑器 / 预览 */}
      <section className="flex-1 flex flex-col min-w-0">
        {sel == null ? (
          <div className="flex-1 flex flex-col items-center justify-center text-center px-6 gap-2 text-mc-faint">
            <div className="text-4xl opacity-50">🗂</div>
            <div className="text-sm text-mc-muted">选一个文件查看 / 编辑</div>
            <div className="text-xs max-w-xs leading-relaxed">
              人格 / 记忆 / 技能 等根目录下的 <code className="text-mc-accent">.md</code> 文件可直接编辑保存
            </div>
          </div>
        ) : (
          <>
            {/* 编辑器头 */}
            <header className="shrink-0 flex items-center gap-2 px-4 py-2 border-b border-mc-border bg-mc-panel/40">
              <span className="text-sm">{fileIcon(sel.name)}</span>
              <span className="text-sm text-mc-text truncate" title={sel.path}>
                {sel.name}
                {dirty && <span className="text-mc-accent ml-1">●</span>}
              </span>
              <span className="text-[10px] text-mc-faint">{fmtSize(sel.size)}</span>
              <div className="flex-1" />
              {isMd(sel.name) && !fileErr && (
                <div className="flex rounded-md border border-mc-border overflow-hidden text-[11px]">
                  {(["edit", "preview"] as const).map((m) => (
                    <button
                      key={m}
                      onClick={() => setMode(m)}
                      className={
                        "px-2.5 py-1 cursor-pointer transition-colors " +
                        (mode === m
                          ? "bg-mc-accent/20 text-mc-accent"
                          : "text-mc-faint hover:text-mc-muted")
                      }
                    >
                      {m === "edit" ? "编辑" : "预览"}
                    </button>
                  ))}
                </div>
              )}
              {readOnly ? (
                <span className="text-[11px] text-mc-faint px-2">只读</span>
              ) : (
                <button
                  onClick={save}
                  disabled={!dirty || saving}
                  className={
                    "text-xs px-3 py-1 rounded-md transition-colors " +
                    (dirty && !saving
                      ? "bg-mc-accent text-white hover:bg-mc-accent-dim cursor-pointer"
                      : "border border-mc-border text-mc-faint cursor-default")
                  }
                >
                  {saving ? "保存中…" : dirty ? "保存" : "已保存"}
                </button>
              )}
            </header>

            {/* 编辑器体 */}
            {fileErr ? (
              <div className="p-4 text-xs text-mc-err">{fileErr}</div>
            ) : mode === "preview" && isMd(sel.name) ? (
              <div className="flex-1 overflow-auto p-5">
                <Suspense fallback={<div className="text-mc-faint text-xs">渲染中…</div>}>
                  <div className="mc-md max-w-3xl">
                    <Markdown text={content} />
                  </div>
                </Suspense>
              </div>
            ) : (
              <textarea
                value={content}
                onChange={(e) => setContent(e.target.value)}
                readOnly={readOnly}
                spellCheck={false}
                className="flex-1 w-full resize-none bg-mc-bg text-mc-text font-mono text-[13px] leading-relaxed p-4 outline-none border-0 focus:ring-0"
              />
            )}
            {/* 状态栏 */}
            {!fileErr && (
              <footer className="shrink-0 px-4 py-1 border-t border-mc-border text-[10px] text-mc-faint flex items-center gap-3">
                <span>{content.length} 字符</span>
                <span>{content.split("\n").length} 行</span>
                {readOnly && <span className="text-mc-warn">· 此类型只读</span>}
                {dirty && !readOnly && <span className="text-mc-accent">· 未保存</span>}
              </footer>
            )}
          </>
        )}
      </section>
    </div>
  );
}

function TreeNode(props: {
  name: string;
  path: string;
  isDir: boolean;
  depth: number;
  isRoot?: boolean;
  dim?: boolean;
  children_: Record<string, Entry[]>;
  loading: Record<string, boolean>;
  selected: string | null;
  onToggleDir: (path: string) => void;
  onOpenFile: (e: Entry) => void;
}) {
  const { name, path, isDir, depth, isRoot, dim, children_, loading, selected } = props;
  const expanded = !!children_[path];
  const active = selected === path;

  return (
    <div>
      <button
        style={{ paddingLeft: `${10 + depth * 14}px` }}
        onClick={() =>
          isDir
            ? props.onToggleDir(path)
            : props.onOpenFile({ name, path, is_dir: false, size: null })
        }
        className={
          "group w-full text-left pr-2 py-1 flex items-center gap-1.5 cursor-pointer truncate relative " +
          (isRoot ? "mt-0.5 text-[12px] uppercase tracking-wide " : "") +
          (active
            ? "bg-mc-accent/15 text-mc-accent"
            : isRoot
              ? "text-mc-muted hover:text-mc-text"
              : "text-mc-muted hover:bg-mc-border/40 hover:text-mc-text")
        }
        title={path}
      >
        {active && <span className="absolute left-0 top-0 bottom-0 w-0.5 bg-mc-accent" />}
        <span className="text-mc-faint w-3 inline-block text-[10px]">
          {isDir ? (expanded ? "▾" : "▸") : ""}
        </span>
        <span className={"truncate " + (dim ? "opacity-40" : "")}>
          {isRoot ? name : `${isDir ? "📁" : fileIcon(name)} ${name}`}
          {isRoot && dim && <span className="ml-1 text-[9px] lowercase opacity-60">空</span>}
        </span>
      </button>
      {isDir && expanded && (
        <div>
          {loading[path] && (
            <div style={{ paddingLeft: `${24 + depth * 14}px` }} className="text-[11px] text-mc-faint py-0.5">…</div>
          )}
          {(children_[path] || []).length === 0 && !loading[path] && (
            <div style={{ paddingLeft: `${24 + depth * 14}px` }} className="text-[10px] text-mc-faint py-0.5 italic">空目录</div>
          )}
          {(children_[path] || []).map((c) => (
            <TreeNode {...props} key={c.path} name={c.name} path={c.path} isDir={c.is_dir} depth={depth + 1} isRoot={false} dim={false} />
          ))}
        </div>
      )}
    </div>
  );
}
