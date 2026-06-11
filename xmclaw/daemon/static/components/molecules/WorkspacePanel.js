// XMclaw — WorkspacePanel (F1 — 2026-05-30)
//
// Right-side drawer rendered next to the chat stream. Two tabs:
//
//   - 文件 : flat-listed tree (built from /api/v2/session_workspaces/{sid}/tree).
//            click → fetch + render via MarkdownBody (md) or <pre> (code/text).
//   - 改动 : git-log timeline (/.../commits) → click commit → diff (/.../diff)
//            rendered as colorised lines.
//
// Auto-opens on the first ``workspace_file_changed`` event for the
// session (state lives in ``chat.workspace`` populated by
// chat_reducer_secondary). Explicit close sets ``userClosed`` so further
// events don't keep prying it open — Proma feel, not nag-feel.
//
// Polling: we re-fetch the tree on every ``chat.workspace.version`` bump.
// No long-poll WS frame for the panel — the chat WS already delivers
// the changed event; we just react to its store mutation. Cheap.

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback, useMemo } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../../lib/api.js";
import { MarkdownBody } from "./MessageBubbleParts.js";
import { CodeBlock } from "./CodeBlock.js";

// ── file-type routing ─────────────────────────────────────────────
// Every file opens in its most natural form:
//   markdown   → MarkdownBody render
//   html       → live preview in a sandboxed iframe (srcdoc — token never
//                enters the frame) with a 预览/源码 toggle
//   svg        → <img> render (scripts inert in img context) + 源码 toggle
//   image      → <img> via the /raw endpoint
//   pdf        → <iframe> via /raw (browser's built-in viewer)
//   code       → CodeBlock with hljs highlighting (lang mapped below)
//   other text → plain <pre>
//   binary     → placeholder + size info

const _MD_EXTS = new Set(["md", "markdown", "mdx"]);
const _IMG_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif"]);
// ext → hljs language id. Only needs entries where the ext ≠ the hljs id.
const _CODE_LANGS = {
  js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript",
  ts: "typescript", tsx: "typescript",
  py: "python", rb: "ruby", rs: "rust", kt: "kotlin",
  yml: "yaml", sh: "bash", bat: "dos", ps1: "powershell",
  h: "c", hpp: "cpp", cc: "cpp", cxx: "cpp",
  vue: "xml", svelte: "xml", xml: "xml",
  md5: "plaintext",
};
const _CODE_EXTS = new Set([
  "js", "mjs", "cjs", "jsx", "ts", "tsx", "py", "json", "yaml", "yml",
  "css", "scss", "less", "sh", "bash", "ps1", "bat", "toml", "ini",
  "sql", "go", "rs", "java", "c", "cpp", "h", "hpp", "cc", "cxx",
  "rb", "php", "xml", "vue", "svelte", "kt", "swift", "lua", "r",
  "dockerfile", "makefile", "cmake", "gradle", "diff", "patch",
]);

function _ext(p) {
  const base = (p || "").split("/").pop() || "";
  // Extension-less but well-known filenames.
  const lower = base.toLowerCase();
  if (lower === "dockerfile" || lower === "makefile") return lower;
  const i = base.lastIndexOf(".");
  if (i < 0) return "";
  return base.slice(i + 1).toLowerCase();
}

function _kindOf(rel) {
  const e = _ext(rel);
  if (_MD_EXTS.has(e)) return "markdown";
  if (e === "html" || e === "htm") return "html";
  if (e === "svg") return "svg";
  if (_IMG_EXTS.has(e)) return "image";
  if (e === "pdf") return "pdf";
  if (_CODE_EXTS.has(e)) return "code";
  return "text";
}

function _langOf(rel) {
  const e = _ext(rel);
  return _CODE_LANGS[e] || e || "plaintext";
}

function _fmtBytes(n) {
  if (!Number.isFinite(n)) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function _fmtTs(ts) {
  if (!ts) return "";
  try {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch (_) { return ""; }
}

// Per-kind file renderer. ``rawUrl`` carries the pairing token as a
// query param — only handed to passive contexts (img / pdf iframe)
// where embedded content can't execute script to read its own URL.
// HTML preview deliberately uses ``srcdoc`` instead so the token never
// enters the frame; sandbox="allow-scripts" (NO allow-same-origin)
// gives demos working JS while keeping an opaque origin — no cookies,
// no localStorage, no parent DOM.
function FileViewer({ rel, body, rawUrl }) {
  const kind = _kindOf(rel);
  // html / svg get a 预览⇄源码 toggle; default to preview.
  const [mode, setMode] = useState("preview");
  useEffect(() => { setMode("preview"); }, [rel]);

  if (kind === "image") {
    return html`<div class="xmc-ws-media"><img src=${rawUrl} alt=${rel} /></div>`;
  }
  if (kind === "pdf") {
    return html`<iframe class="xmc-ws-pdfframe" src=${rawUrl} title=${rel}></iframe>`;
  }

  const content = (body && body.content) || "";
  if (body && body.kind === "binary") {
    return html`<div class="xmc-ws-hint">二进制文件 — 无文本预览</div>`;
  }

  const toggle = html`
    <div class="xmc-ws-modetabs">
      <button type="button" class=${"xmc-ws-modetab" + (mode === "preview" ? " is-active" : "")}
        onClick=${() => setMode("preview")}>预览</button>
      <button type="button" class=${"xmc-ws-modetab" + (mode === "source" ? " is-active" : "")}
        onClick=${() => setMode("source")}>源码</button>
    </div>
  `;

  if (kind === "html") {
    return html`
      ${toggle}
      ${mode === "preview"
        ? html`<iframe class="xmc-ws-htmlframe" sandbox="allow-scripts" srcdoc=${content} title=${rel}></iframe>`
        : html`<${CodeBlock} code=${content} lang="html" maxLines=${0} />`}
    `;
  }
  if (kind === "svg") {
    return html`
      ${toggle}
      ${mode === "preview"
        ? html`<div class="xmc-ws-media"><img src=${rawUrl} alt=${rel} /></div>`
        : html`<${CodeBlock} code=${content} lang="xml" maxLines=${0} />`}
    `;
  }
  if (kind === "markdown") {
    return html`<div class="xmc-ws-md"><${MarkdownBody} content=${content} /></div>`;
  }
  if (kind === "code") {
    return html`<${CodeBlock} code=${content} lang=${_langOf(rel)} maxLines=${0} />`;
  }
  return html`<pre class="xmc-ws-code">${content}</pre>`;
}

// Render a git unified diff with red/green line colours.
function DiffView({ diff }) {
  const lines = (diff || "").split("\n");
  return html`
    <pre class="xmc-ws-diff">
      ${lines.map((ln, i) => {
        let cls = "";
        if (ln.startsWith("+++") || ln.startsWith("---") || ln.startsWith("diff ")) cls = "is-head";
        else if (ln.startsWith("@@")) cls = "is-hunk";
        else if (ln.startsWith("+")) cls = "is-add";
        else if (ln.startsWith("-")) cls = "is-del";
        return html`<div key=${i} class=${"xmc-ws-diff-line " + cls}>${ln || " "}</div>`;
      })}
    </pre>
  `;
}

export function WorkspacePanel({ token, sid, workspaceState, onUserClose }) {
  const ws = workspaceState || { version: 0, lastChange: null, opened: false };
  const opened = !!ws.opened;
  const [tab, setTab] = useState("files");
  const [entries, setEntries] = useState([]);
  const [selected, setSelected] = useState(null);
  const [fileBody, setFileBody] = useState(null);
  const [commits, setCommits] = useState([]);
  const [selectedSha, setSelectedSha] = useState(null);
  const [diff, setDiff] = useState(null);
  const [loading, setLoading] = useState(false);

  const fetchTree = useCallback(async () => {
    if (!sid || !token || !opened) return;
    try {
      const r = await apiGet(`/api/v2/session_workspaces/${encodeURIComponent(sid)}/tree`, token);
      if (r && r.ok) setEntries(r.entries || []);
    } catch (_) { /* swallow — panel is best-effort */ }
  }, [sid, token, opened]);

  const fetchCommits = useCallback(async () => {
    if (!sid || !token || !opened) return;
    try {
      const r = await apiGet(`/api/v2/session_workspaces/${encodeURIComponent(sid)}/commits?limit=50`, token);
      if (r && r.ok) setCommits(r.commits || []);
    } catch (_) { /* */ }
  }, [sid, token, opened]);

  // Refetch whenever a change event lands (version bump) or the user
  // first opens the drawer (opened transition).
  useEffect(() => {
    if (!opened) return;
    if (tab === "files") fetchTree();
    if (tab === "changes") fetchCommits();
  }, [opened, tab, ws.version, fetchTree, fetchCommits]);

  // Reset selections when session changes — stale file body from a
  // previous session would be confusing.
  useEffect(() => {
    setSelected(null); setFileBody(null);
    setSelectedSha(null); setDiff(null);
  }, [sid]);

  const onPickFile = useCallback(async (rel) => {
    setSelected(rel);
    const kind = _kindOf(rel);
    // Media kinds render straight off the /raw endpoint — no point
    // round-tripping megabytes of image bytes through the JSON /file
    // route just to throw them away.
    if (kind === "image" || kind === "pdf") {
      setFileBody({ loading: false, media: true });
      return;
    }
    setFileBody({ loading: true });
    try {
      const r = await apiGet(
        `/api/v2/session_workspaces/${encodeURIComponent(sid)}/file?path=${encodeURIComponent(rel)}`,
        token,
      );
      if (r && r.ok) setFileBody({ loading: false, content: r.content || "", kind: r.kind, truncated: r.truncated, bytes: r.bytes });
      else setFileBody({ loading: false, error: r && r.error || "read_failed" });
    } catch (e) { setFileBody({ loading: false, error: String(e) }); }
  }, [sid, token]);

  const onPickCommit = useCallback(async (sha) => {
    setSelectedSha(sha); setDiff({ loading: true });
    try {
      const r = await apiGet(
        `/api/v2/session_workspaces/${encodeURIComponent(sid)}/diff?commit=${encodeURIComponent(sha)}`,
        token,
      );
      if (r && r.ok) setDiff({ loading: false, body: r.diff || "" });
      else setDiff({ loading: false, error: r && r.error || "diff_failed" });
    } catch (e) { setDiff({ loading: false, error: String(e) }); }
  }, [sid, token]);

  // The drawer collapses to a thin tab-strip when closed so the user
  // can re-open it with one click after dismissing.
  if (!opened) {
    if (!ws.lastChange) return null;
    return html`
      <button
        type="button"
        class="xmc-ws-reopen"
        title="点击展开工作区"
        onClick=${() => onUserClose && onUserClose(false)}
      >📂 工作区 · ${ws.lastChange.rel_path}</button>
    `;
  }

  return html`
    <aside class="xmc-ws-panel" aria-label="session workspace">
      <header class="xmc-ws-head">
        <div class="xmc-ws-tabs">
          <button type="button"
            class=${"xmc-ws-tab" + (tab === "files" ? " is-active" : "")}
            onClick=${() => setTab("files")}>文件</button>
          <button type="button"
            class=${"xmc-ws-tab" + (tab === "changes" ? " is-active" : "")}
            onClick=${() => setTab("changes")}>改动</button>
        </div>
        <div class="xmc-ws-meta">
          ${ws.lastChange ? html`<span class="xmc-ws-last">${ws.lastChange.action} · ${_fmtTs(ws.lastChange.ts)}</span>` : null}
          <button type="button" class="xmc-ws-close" title="收起" onClick=${() => onUserClose && onUserClose(true)}>×</button>
        </div>
      </header>

      ${tab === "files" ? html`
        <div class="xmc-ws-body">
          <div class="xmc-ws-tree">
            ${entries.length === 0
              ? html`<div class="xmc-ws-empty">还没有文件 — agent 写入后会自动出现在这里。</div>`
              : entries.filter((e) => e.kind === "file").map((e) => html`
                  <button type="button" key=${e.rel_path}
                    class=${"xmc-ws-treeitem" + (selected === e.rel_path ? " is-active" : "")}
                    onClick=${() => onPickFile(e.rel_path)}>
                    <span class="xmc-ws-treename">${e.rel_path}</span>
                    <span class="xmc-ws-treesize">${_fmtBytes(e.size)}</span>
                  </button>
                `)}
          </div>
          <div class="xmc-ws-viewer">
            ${selected === null
              ? html`<div class="xmc-ws-hint">点左侧文件查看内容</div>`
              : fileBody && fileBody.loading
                ? html`<div class="xmc-ws-hint">读取中…</div>`
                : fileBody && fileBody.error
                  ? html`<div class="xmc-ws-error">读取失败：${fileBody.error}</div>`
                  : fileBody
                    ? html`
                        <div class="xmc-ws-viewer-head">
                          <code>${selected}</code>
                          <span>
                            ${fileBody.bytes ? _fmtBytes(fileBody.bytes) : ""}${fileBody.truncated ? " · 已截断" : ""}
                            <a class="xmc-ws-dl" href=${`/api/v2/session_workspaces/${encodeURIComponent(sid)}/raw?path=${encodeURIComponent(selected)}&token=${encodeURIComponent(token || "")}`}
                              download=${selected.split("/").pop()} title="下载">⬇</a>
                          </span>
                        </div>
                        <${FileViewer}
                          rel=${selected}
                          body=${fileBody}
                          rawUrl=${`/api/v2/session_workspaces/${encodeURIComponent(sid)}/raw?path=${encodeURIComponent(selected)}&token=${encodeURIComponent(token || "")}`}
                        />
                      `
                    : null}
          </div>
        </div>
      ` : html`
        <div class="xmc-ws-body">
          <div class="xmc-ws-tree">
            ${commits.length === 0
              ? html`<div class="xmc-ws-empty">还没有变更记录。</div>`
              : commits.map((c) => html`
                  <button type="button" key=${c.sha}
                    class=${"xmc-ws-treeitem" + (selectedSha === c.sha ? " is-active" : "")}
                    onClick=${() => onPickCommit(c.sha)}>
                    <span class="xmc-ws-treename">${c.subject || "(no subject)"}</span>
                    <span class="xmc-ws-treesize">${_fmtTs(c.ts)}</span>
                  </button>
                `)}
          </div>
          <div class="xmc-ws-viewer">
            ${selectedSha === null
              ? html`<div class="xmc-ws-hint">点左侧 commit 查看 diff</div>`
              : diff && diff.loading
                ? html`<div class="xmc-ws-hint">读取中…</div>`
                : diff && diff.error
                  ? html`<div class="xmc-ws-error">读取失败：${diff.error}</div>`
                  : diff
                    ? html`<${DiffView} diff=${diff.body || ""} />`
                    : null}
          </div>
        </div>
      `}
    </aside>
  `;
}
