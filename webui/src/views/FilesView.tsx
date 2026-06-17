// 文件域：工作区 markdown 文件树 + 编辑器（2026-06-17 用户点名「在前端加上
// md 文件树，我自己修改」）。读/写走 /api/v2/files（GET 列目录/读文件，
// PUT 写文件，限 personas/memory/skills/agents/workspaces 根）。
import { useEffect, useState } from "react";
import { useApp } from "../store/app";
import { apiGet, apiSend } from "../lib/api";

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

const isTextEditable = (name: string) =>
  /\.(md|markdown|txt|json|ya?ml|toml|csv|py|js|ts|tsx|css|html|env|ini|cfg)$/i.test(name);

export default function FilesView() {
  const token = useApp((s) => s.token);
  const showToast = useApp((s) => s.showToast);

  const [roots, setRoots] = useState<Root[]>([]);
  // path -> child entries (presence = expanded). undefined = collapsed.
  const [children, setChildren] = useState<Record<string, Entry[]>>({});
  const [loading, setLoading] = useState<Record<string, boolean>>({});

  const [sel, setSel] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [original, setOriginal] = useState("");
  const [readOnly, setReadOnly] = useState(false);
  const [saving, setSaving] = useState(false);
  const [fileErr, setFileErr] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    apiGet<{ roots: Root[] }>("/api/v2/files/roots", token)
      .then((d) => setRoots(d.roots || []))
      .catch(() => setRoots([]));
  }, [token]);

  async function loadDir(path: string) {
    if (!token) return;
    if (children[path]) {
      // collapse
      setChildren((c) => {
        const n = { ...c };
        delete n[path];
        return n;
      });
      return;
    }
    setLoading((l) => ({ ...l, [path]: true }));
    try {
      const d = await apiGet<{ entries?: Entry[] }>(
        `/api/v2/files?path=${encodeURIComponent(path)}`,
        token,
      );
      setChildren((c) => ({ ...c, [path]: d.entries || [] }));
    } catch {
      showToast("目录读取失败", "err");
    } finally {
      setLoading((l) => ({ ...l, [path]: false }));
    }
  }

  async function openFile(e: Entry) {
    if (!token) return;
    setSel(e.path);
    setFileErr(null);
    setReadOnly(!isTextEditable(e.name));
    try {
      const d = await apiGet<{ content?: string; size?: number }>(
        `/api/v2/files?path=${encodeURIComponent(e.path)}`,
        token,
      );
      setContent(d.content ?? "");
      setOriginal(d.content ?? "");
    } catch {
      setFileErr("文件读取失败（可能超过 1 MiB 或非文本）");
      setContent("");
      setOriginal("");
    }
  }

  async function save() {
    if (!token || sel == null) return;
    setSaving(true);
    try {
      await apiSend("PUT", "/api/v2/files", { path: sel, content }, token);
      setOriginal(content);
      showToast("已保存", "ok");
    } catch (err) {
      const msg = (err as Error)?.message || "";
      showToast(
        msg.includes("403") ? "该文件不在可写根内，拒绝写入" : "保存失败",
        "err",
      );
    } finally {
      setSaving(false);
    }
  }

  const dirty = content !== original;

  return (
    <div className="flex-1 flex min-h-0">
      {/* 左：文件树 */}
      <div className="w-72 shrink-0 border-r border-mc-border overflow-auto py-2 text-sm">
        <div className="px-3 pb-2 text-[11px] uppercase tracking-wide text-mc-faint">
          工作区文件
        </div>
        {roots.length === 0 && (
          <div className="px-3 text-mc-faint text-xs">加载中…</div>
        )}
        {roots.map((r) => (
          <TreeNode
            key={r.path}
            name={`${r.label}`}
            path={r.path}
            isDir
            depth={0}
            children_={children}
            loading={loading}
            selected={sel}
            onToggleDir={loadDir}
            onOpenFile={openFile}
          />
        ))}
      </div>

      {/* 右：编辑器 */}
      <div className="flex-1 flex flex-col min-w-0">
        {sel == null ? (
          <div className="flex-1 flex items-center justify-center text-mc-faint text-sm">
            选一个文件来查看 / 编辑（personas / memory / skills 等根下的 .md 可编辑）
          </div>
        ) : (
          <>
            <div className="shrink-0 flex items-center gap-3 px-4 py-2 border-b border-mc-border">
              <span className="text-xs text-mc-muted truncate flex-1" title={sel}>
                {sel}
                {dirty && <span className="text-mc-accent"> ●</span>}
              </span>
              {readOnly ? (
                <span className="text-[11px] text-mc-faint">只读（非文本/不可编辑类型）</span>
              ) : (
                <button
                  onClick={save}
                  disabled={!dirty || saving}
                  className={
                    "text-xs px-3 py-1 rounded border border-mc-border " +
                    (dirty && !saving
                      ? "text-mc-accent hover:bg-mc-accent/10 cursor-pointer"
                      : "text-mc-faint cursor-default")
                  }
                >
                  {saving ? "保存中…" : "保存"}
                </button>
              )}
            </div>
            {fileErr ? (
              <div className="p-4 text-xs text-mc-err">{fileErr}</div>
            ) : (
              <textarea
                value={content}
                onChange={(e) => setContent(e.target.value)}
                readOnly={readOnly}
                spellCheck={false}
                className="flex-1 w-full resize-none bg-transparent text-mc-text font-mono text-xs leading-relaxed p-4 outline-none"
              />
            )}
          </>
        )}
      </div>
    </div>
  );
}

function TreeNode(props: {
  name: string;
  path: string;
  isDir: boolean;
  depth: number;
  children_: Record<string, Entry[]>;
  loading: Record<string, boolean>;
  selected: string | null;
  onToggleDir: (path: string) => void;
  onOpenFile: (e: Entry) => void;
}) {
  const { name, path, isDir, depth, children_, loading, selected } = props;
  const expanded = !!children_[path];
  const pad = { paddingLeft: `${8 + depth * 14}px` };

  return (
    <div>
      <button
        style={pad}
        onClick={() =>
          isDir
            ? props.onToggleDir(path)
            : props.onOpenFile({ name, path, is_dir: false, size: null })
        }
        className={
          "w-full text-left pr-2 py-1 flex items-center gap-1.5 cursor-pointer truncate " +
          (selected === path
            ? "bg-mc-accent/10 text-mc-accent"
            : "text-mc-muted hover:bg-mc-border/40")
        }
        title={name}
      >
        <span className="text-mc-faint w-3 inline-block">
          {isDir ? (expanded ? "▾" : "▸") : ""}
        </span>
        <span className="truncate">{isDir ? `📁 ${name}` : `📄 ${name}`}</span>
      </button>
      {isDir && expanded && (
        <div>
          {loading[path] && (
            <div style={{ paddingLeft: `${22 + depth * 14}px` }} className="text-[11px] text-mc-faint py-0.5">
              …
            </div>
          )}
          {(children_[path] || []).map((c) => (
            <TreeNode
              {...props}
              key={c.path}
              name={c.name}
              path={c.path}
              isDir={c.is_dir}
              depth={depth + 1}
            />
          ))}
        </div>
      )}
    </div>
  );
}
