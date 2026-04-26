// XMclaw — Workspace file panel
//
// OpenClaw-style sidebar tree of editable artifacts (skills / agents /
// personas / memory / workspaces).
//
// Backed by /api/v2/files: GET /roots lists the conceptual roots, GET ""
// with ?path= lists a directory or reads a file (≤1 MiB), PUT "" with
// {path, content} writes back atomically (workspace roots only).

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet, apiPut } from "../lib/api.js";

function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function FileRow({ entry, depth, isActive, onPick, onToggleDir }) {
  const indent = { paddingLeft: `${depth * 14}px` };
  if (entry.is_dir) {
    return html`
      <li class="xmc-ws__row xmc-ws__row--dir ${entry._open ? "is-open" : ""}"
          style=${indent}
          onClick=${() => onToggleDir(entry)}>
        <span class="xmc-ws__caret">${entry._open ? "▾" : "▸"}</span>
        <span class="xmc-ws__name">${entry.name}/</span>
      </li>
    `;
  }
  return html`
    <li class="xmc-ws__row xmc-ws__row--file ${isActive ? "is-active" : ""}"
        style=${indent}
        tabindex="0"
        role="button"
        onClick=${() => onPick(entry)}
        onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick(entry); } }}>
      <span class="xmc-ws__caret"> </span>
      <span class="xmc-ws__name">${entry.name}</span>
      <small class="xmc-ws__meta">${fmtSize(entry.size)}</small>
    </li>
  `;
}

function RootNode({ root, openMap, entriesMap, errorMap, onToggleRoot, onToggleDir, onPick, activePath }) {
  const isOpen = !!openMap[root.path];
  const entries = entriesMap[root.path];
  const err = errorMap[root.path];
  const indent = { paddingLeft: "0px" };
  return html`
    <li class="xmc-ws__root">
      <div class="xmc-ws__root-header" style=${indent} onClick=${() => onToggleRoot(root)}>
        <span class="xmc-ws__caret">${isOpen ? "▾" : "▸"}</span>
        <strong>${root.label}</strong>
        ${root.exists ? null : html`<small class="xmc-ws__missing">(空)</small>`}
      </div>
      ${isOpen ? html`
        <ul class="xmc-ws__children">
          ${err ? html`<li class="xmc-ws__error">${err}</li>` : null}
          ${!err && entries == null ? html`<li class="xmc-ws__loading">加载中…</li>` : null}
          ${!err && entries != null && entries.length === 0
            ? html`<li class="xmc-ws__empty">空目录</li>`
            : null}
          ${!err && entries != null
            ? entries.map((e) => html`
                <${FileRow}
                  key=${e.path}
                  entry=${e}
                  depth=${1}
                  isActive=${activePath === e.path}
                  onPick=${onPick}
                  onToggleDir=${onToggleDir}
                />
                ${e.is_dir && e._open && e._children != null ? e._children.map((c) => html`
                  <${FileRow}
                    key=${c.path}
                    entry=${c}
                    depth=${2}
                    isActive=${activePath === c.path}
                    onPick=${onPick}
                    onToggleDir=${onToggleDir}
                  />
                `) : null}
              `)
            : null}
        </ul>
      ` : null}
    </li>
  `;
}

export function WorkspacePage({ token }) {
  const [roots, setRoots] = useState(null);
  const [error, setError] = useState(null);
  const [openMap, setOpenMap] = useState({});       // path -> bool (root + dirs)
  const [entriesMap, setEntriesMap] = useState({}); // path -> [entry,...]
  const [errorMap, setErrorMap] = useState({});     // path -> "..."
  const [activePath, setActivePath] = useState(null);
  const [viewer, setViewer] = useState(null);       // {path,size,content} | "loading" | {error}
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [savedAt, setSavedAt] = useState(null);

  // Workspace roots — used to gate the Edit button so the user can't
  // try to edit a $HOME file the backend will refuse.
  const isUnderWorkspaceRoot = (path) => {
    if (!path || !roots) return false;
    return roots.some((r) => path === r.path || path.startsWith(r.path + "/") || path.startsWith(r.path + "\\"));
  };

  useEffect(() => {
    let cancelled = false;
    apiGet("/api/v2/files/roots", token)
      .then((d) => { if (!cancelled) setRoots(d.roots || []); })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [token]);

  const fetchListing = (path) => {
    return apiGet(`/api/v2/files?path=${encodeURIComponent(path)}`, token)
      .then((d) => {
        if (!d.is_dir) return [];
        return (d.entries || []).map((e) => ({ ...e, _open: false, _children: null }));
      });
  };

  const toggleRoot = (root) => {
    const path = root.path;
    const next = !openMap[path];
    setOpenMap({ ...openMap, [path]: next });
    // Fresh-install roots that don't exist on disk yet: short-circuit
    // to an empty listing so we never paint a red 404 next to "(空)".
    if (next && entriesMap[path] == null && !errorMap[path]) {
      if (!root.exists) {
        setEntriesMap((m) => ({ ...m, [path]: [] }));
        return;
      }
      fetchListing(path)
        .then((entries) => setEntriesMap((m) => ({ ...m, [path]: entries })))
        .catch((e) => setErrorMap((m) => ({ ...m, [path]: String(e.message || e) })));
    }
  };

  const toggleDir = (entry) => {
    // Walk entriesMap and flip _open / load _children for the matched path.
    setEntriesMap((m) => {
      const next = { ...m };
      for (const rootPath of Object.keys(next)) {
        const arr = next[rootPath];
        if (!arr) continue;
        const updated = arr.map((e) => {
          if (e.path === entry.path) {
            const open = !e._open;
            if (open && e._children == null) {
              fetchListing(e.path)
                .then((children) => {
                  setEntriesMap((mm) => ({
                    ...mm,
                    [rootPath]: mm[rootPath].map((x) =>
                      x.path === e.path ? { ...x, _children: children } : x
                    ),
                  }));
                })
                .catch((err) => {
                  setEntriesMap((mm) => ({
                    ...mm,
                    [rootPath]: mm[rootPath].map((x) =>
                      x.path === e.path ? { ...x, _children: [{ name: `(error: ${String(err.message || err)})`, path: `${e.path}/__err`, is_dir: false, size: null }] } : x
                    ),
                  }));
                });
            }
            return { ...e, _open: open };
          }
          return e;
        });
        next[rootPath] = updated;
      }
      return next;
    });
  };

  const pickFile = (entry) => {
    setActivePath(entry.path);
    setViewer("loading");
    setEditing(false);
    setSaveError(null);
    setSavedAt(null);
    apiGet(`/api/v2/files?path=${encodeURIComponent(entry.path)}`, token)
      .then((d) => setViewer(d))
      .catch((e) => setViewer({ error: String(e.message || e) }));
  };

  const startEdit = () => {
    if (!viewer || viewer === "loading" || viewer.error) return;
    setDraft(viewer.content || "");
    setEditing(true);
    setSaveError(null);
    setSavedAt(null);
  };

  const cancelEdit = () => {
    setEditing(false);
    setDraft("");
    setSaveError(null);
  };

  const onSave = async () => {
    if (!activePath) return;
    setSaving(true);
    setSaveError(null);
    try {
      const res = await apiPut("/api/v2/files", {
        path: activePath, content: draft,
      }, token);
      setViewer({ ...viewer, content: draft, size: res.size_after });
      setEditing(false);
      setSavedAt(Date.now());
    } catch (exc) {
      setSaveError(String(exc.message || exc));
    } finally {
      setSaving(false);
    }
  };

  if (error) return html`<section class="xmc-datapage"><h2>工作区</h2><p class="xmc-datapage__error">${error}</p></section>`;
  if (!roots) return html`<section class="xmc-datapage"><p>加载中…</p></section>`;

  return html`
    <section class="xmc-datapage xmc-datapage--split" aria-labelledby="ws-title">
      <header class="xmc-datapage__header">
        <h2 id="ws-title">工作区</h2>
        <p class="xmc-datapage__subtitle">浏览 / 编辑技能 / 智能体 / 人格 / 记忆 / 工作区配置文件。$HOME 内可读，写入仅限工作区根目录。</p>
      </header>
      <div class="xmc-datapage__split">
        <aside class="xmc-datapage__sidebar">
          <ul class="xmc-ws__tree">
            ${roots.map((r) => html`
              <${RootNode}
                key=${r.path}
                root=${r}
                openMap=${openMap}
                entriesMap=${entriesMap}
                errorMap=${errorMap}
                onToggleRoot=${toggleRoot}
                onToggleDir=${toggleDir}
                onPick=${pickFile}
                activePath=${activePath}
              />
            `)}
          </ul>
        </aside>
        <article class="xmc-datapage__viewer">
          ${viewer === null ? html`<p class="xmc-datapage__hint">展开任一类别，从中选择文件查看。</p>` : null}
          ${viewer === "loading" ? html`<p>加载中…</p>` : null}
          ${viewer && viewer.error ? html`<p class="xmc-datapage__error">${viewer.error}</p>` : null}
          ${viewer && viewer.content != null ? html`
            <header class="xmc-datapage__viewer-header">
              <h3>${activePath}</h3>
              <small>${fmtSize(editing ? new Blob([draft]).size : viewer.size)}</small>
              <div style="margin-left:auto;display:flex;gap:.5rem;align-items:center">
                ${savedAt ? html`<small style="color:var(--xmc-success)">已保存</small>` : null}
                ${saveError ? html`<small style="color:var(--xmc-error)">${saveError}</small>` : null}
                ${editing ? html`
                  <button type="button" onClick=${cancelEdit} disabled=${saving}>取消</button>
                  <button type="button" onClick=${onSave} disabled=${saving || draft === viewer.content}>${saving ? "保存中…" : "保存"}</button>
                ` : html`
                  ${isUnderWorkspaceRoot(activePath)
                    ? html`<button type="button" onClick=${startEdit}>编辑</button>`
                    : html`<small style="color:var(--xmc-fg-muted)">只读 ($HOME 外只可查看)</small>`}
                `}
              </div>
            </header>
            ${editing
              ? html`<textarea
                    class="xmc-datapage__viewer-body"
                    style="width:100%;min-height:480px;font-family:var(--xmc-font-mono);font-size:var(--xmc-font-size-sm);resize:vertical"
                    value=${draft}
                    onInput=${(e) => setDraft(e.target.value)}
                    spellcheck="false"
                  />`
              : html`<pre class="xmc-datapage__viewer-body">${viewer.content}</pre>`}
          ` : null}
        </article>
      </div>
    </section>
  `;
}
