// XMclaw — Files page (Iteration 5 + 2026-05-12 roots panel)
//
// File browser + editor. Reads directories and text files via
// /api/v2/files, writes back via PUT.
//
// Sidebar shows two groups:
//   1. ``XMclaw 数据`` — the 5 canonical workspace roots from
//      ``xmclaw.utils.paths`` (skills / agents / personas / memory /
//      workspaces). System-defined, not user-modifiable.
//   2. ``可浏览目录 (tools.allowed_dirs)`` — additional roots the
//      user has whitelisted in ``daemon/config.json``. Read from
//      ``/api/v2/config`` so the page reflects current live config
//      (hot-reload aware). The "添加…" button opens a dialog
//      explaining how to edit the config — daemon-side mutation
//      endpoint is intentionally NOT exposed (writes to config.json
//      are reserved for the user / the Settings page).

const { h } = window.__xmc.preact;
const { useEffect, useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet, apiPut } from "../lib/api.js";
import { toast } from "../lib/toast.js";
import { confirmDialog } from "../lib/dialog.js";

function _basename(p) {
  const parts = String(p).replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : p;
}

function _dirname(p) {
  const s = String(p).replace(/\\/g, "/");
  const i = s.lastIndexOf("/");
  return i > 0 ? s.slice(0, i) : "";
}

export function FilesPage({ token }) {
  const [roots, setRoots] = useState([]);
  const [allowedDirs, setAllowedDirs] = useState([]);
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState(null);
  const [content, setContent] = useState("");
  const [isDir, setIsDir] = useState(true);
  const [loading, setLoading] = useState(false);
  const [dirty, setDirty] = useState(false);

  async function loadRoots() {
    try {
      const d = await apiGet("/api/v2/files/roots", token);
      setRoots(d.roots || []);
      if (!path && d.roots.length) setPath(d.roots[0].path);
    } catch (_) {}
  }

  // Pull tools.allowed_dirs from the live config so the second sidebar
  // group reflects what the user has actually whitelisted. Resilient
  // to missing fields — the sanitised config snapshot always has the
  // structure ``{config: {tools: {allowed_dirs: [...]}}}`` once tools
  // are configured, and is ``{config: null, note: ...}`` for echo-mode
  // installs.
  async function loadAllowedDirs() {
    try {
      const d = await apiGet("/api/v2/config", token);
      const dirs = ((d && d.config && d.config.tools) || {}).allowed_dirs || [];
      setAllowedDirs(Array.isArray(dirs) ? dirs : []);
    } catch (_) {
      setAllowedDirs([]);
    }
  }

  async function explainAddRoot() {
    // We deliberately don't expose POST /api/v2/files/roots — mutating
    // config from a UI button + persisting back to config.json on disk
    // is a separate concern (auth, atomic write, hot-reload race). For
    // now we just teach the user how to do it themselves via the file
    // they already have edit access to.
    await confirmDialog({
      title: "添加可浏览目录",
      body: (
        "想让 Files 页能浏览其他目录? 编辑 ``daemon/config.json``:\n\n" +
        "  \"tools\": {\n" +
        "    \"allowed_dirs\": [\n" +
        "      \"~/projects/foo\",\n" +
        "      \"D:/work/notes\"\n" +
        "    ]\n" +
        "  }\n\n" +
        "保存后 daemon 会热重载, 刷新本页即可看到新目录。\n\n" +
        "注意: allowed_dirs 控制的是浏览权限, 不是写权限 — 写入仍只能落到\n" +
        "XMclaw 5 个 canonical roots (上方 ``XMclaw 数据`` 那组)。"
      ),
      confirmLabel: "知道了",
    });
  }

  async function loadPath(p) {
    setLoading(true);
    try {
      const d = await apiGet("/api/v2/files?path=" + encodeURIComponent(p), token);
      setPath(d.path || p);
      setIsDir(!!d.is_dir);
      if (d.is_dir) {
        setEntries(d.entries || []);
        setContent("");
        setDirty(false);
      } else {
        setEntries(null);
        setContent(d.content || "");
        setDirty(false);
      }
    } catch (e) {
      toast.error("读取失败: " + String(e.message || e));
    } finally {
      setLoading(false);
    }
  }

  async function save() {
    try {
      await apiPut("/api/v2/files", { path, content }, token);
      toast.success("已保存");
      setDirty(false);
    } catch (e) {
      toast.error("保存失败: " + String(e.message || e));
    }
  }

  useEffect(() => { loadRoots(); loadAllowedDirs(); }, [token]);
  useEffect(() => { if (path) loadPath(path); }, [path]);

  const crumbs = [];
  const parts = String(path).replace(/\\/g, "/").split("/").filter(Boolean);
  let acc = "";
  for (const part of parts) {
    acc = acc ? acc + "/" + part : part;
    crumbs.push({ label: part, path: acc });
  }

  return html`
    <section class="xmc-page" aria-labelledby="files-title">
      <header class="xmc-page__head">
        <h1 id="files-title">文件</h1>
      </header>

      <div style="display:flex;gap:1rem;flex-wrap:wrap">
        <!-- Sidebar: roots -->
        <div style="min-width:200px;max-width:260px;flex:1">
          <!-- Group 1: canonical XMclaw workspace roots -->
          <div style="font-size:.75rem;opacity:.6;margin-bottom:.4rem">XMclaw 数据</div>
          <ul style="list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.15rem">
            ${roots.map((r) => html`
              <li key=${r.key}>
                <button onClick=${() => setPath(r.path)} style="width:100%;text-align:left;padding:.35rem .5rem;background:${path.startsWith(r.path)?"rgba(255,255,255,.06)":"transparent"};border:1px solid ${path.startsWith(r.path)?"var(--color-border)":"transparent"};border-radius:4px;color:inherit;cursor:pointer;font-size:.82rem;${r.exists===false?"opacity:.55":""}">
                  ${r.label}${r.exists === false ? " ·" : ""}${r.exists === false ? html`<span style="opacity:.7;font-size:.7rem"> 未创建</span>` : null}
                </button>
              </li>
            `)}
          </ul>

          <!-- Group 2: user-configured allowed_dirs (read from /api/v2/config) -->
          <div style="font-size:.75rem;opacity:.6;margin:.9rem 0 .4rem;display:flex;align-items:center;justify-content:space-between">
            <span>可浏览目录</span>
            <button
              type="button"
              onClick=${explainAddRoot}
              title="如何添加可浏览目录?"
              style="background:none;border:1px solid var(--color-border);border-radius:50%;width:1.3rem;height:1.3rem;cursor:pointer;color:inherit;font-size:.7rem;line-height:1;padding:0">＋</button>
          </div>
          ${allowedDirs.length === 0
            ? html`<div style="font-size:.72rem;opacity:.5;padding:.35rem .5rem">
                (无) ·
                <button type="button" onClick=${explainAddRoot}
                  style="background:none;border:0;color:var(--xmc-accent);cursor:pointer;padding:0;font:inherit">怎么加?</button>
              </div>`
            : html`<ul style="list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.15rem">
                ${allowedDirs.map((d, i) => html`
                  <li key=${i}>
                    <button onClick=${() => setPath(d)} style="width:100%;text-align:left;padding:.35rem .5rem;background:${path.startsWith(d)?"rgba(255,255,255,.06)":"transparent"};border:1px solid ${path.startsWith(d)?"var(--color-border)":"transparent"};border-radius:4px;color:inherit;cursor:pointer;font-size:.78rem;font-family:var(--xmc-font-mono);word-break:break-all"
                      title=${d}>
                      ${d}
                    </button>
                  </li>
                `)}
              </ul>`}
        </div>

        <!-- Main -->
        <div style="flex:3;min-width:300px">
          <!-- Breadcrumb -->
          <div style="display:flex;align-items:center;gap:.3rem;font-size:.82rem;margin-bottom:.6rem;flex-wrap:wrap">
            ${crumbs.map((c, i) => html`
              <span key=${i}>
                ${i > 0 ? html`<span style="opacity:.4;margin:0 .2rem">/</span>` : null}
                <button onClick=${() => setPath(c.path)} style="background:none;border:none;color:var(--xmc-accent);cursor:pointer;padding:0;font-size:inherit">
                  ${c.label}
                </button>
              </span>
            `)}
            ${!isDir && dirty ? html`<span style="margin-left:auto;color:var(--xmc-warn);font-size:.75rem">● 未保存</span>` : null}
            ${!isDir ? html`<button onClick=${save} style="margin-left:auto;font-size:.75rem;padding:.3rem .7rem">保存</button>` : null}
          </div>

          ${loading ? html`<div style="padding:2rem;text-align:center">加载中…</div>` : null}

          ${isDir && entries
            ? html`<ul style="list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.1rem">
                ${entries.map((e) => html`
                  <li key=${e.path}>
                    <button onClick=${() => setPath(e.path)} style="width:100%;text-align:left;padding:.4rem .5rem;background:transparent;border:0;border-radius:4px;color:inherit;cursor:pointer;font-size:.85rem;display:flex;align-items:center;gap:.5rem">
                      <span style="opacity:.7">${e.is_dir ? "📁" : "📄"}</span>
                      <span>${e.name}</span>
                      ${e.size != null ? html`<span style="margin-left:auto;font-size:.75rem;opacity:.5;font-family:var(--xmc-font-mono)">${e.size} B</span>` : null}
                    </button>
                  </li>
                `)}
              </ul>`
            : null}

          ${!isDir
            ? html`<textarea value=${content} onInput=${e => { setContent(e.target.value); setDirty(true); }}
                style="width:100%;min-height:60vh;font-family:var(--xmc-font-mono);font-size:.85rem;line-height:1.5;padding:.6rem;border:1px solid var(--color-border);border-radius:6px;background:var(--color-surface);color:inherit;resize:vertical"
                spellcheck="false" />`
            : null}
        </div>
      </div>
    </section>
  `;
}
