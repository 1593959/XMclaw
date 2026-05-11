// XMclaw — Files page (Iteration 5)
//
// File browser + editor. Reads directories and text files via
// /api/v2/files, writes back via PUT.

const { h } = window.__xmc.preact;
const { useEffect, useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet, apiPut } from "../lib/api.js";
import { toast } from "../lib/toast.js";

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

  useEffect(() => { loadRoots(); }, [token]);
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
        <div style="min-width:180px;max-width:240px;flex:1">
          <div style="font-size:.75rem;opacity:.6;margin-bottom:.4rem">工作区</div>
          <ul style="list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:.15rem">
            ${roots.map((r) => html`
              <li key=${r.key}>
                <button onClick=${() => setPath(r.path)} style="width:100%;text-align:left;padding:.35rem .5rem;background:${path.startsWith(r.path)?"rgba(255,255,255,.06)":"transparent"};border:1px solid ${path.startsWith(r.path)?"var(--color-border)":"transparent"};border-radius:4px;color:inherit;cursor:pointer;font-size:.82rem">
                  ${r.label}
                </button>
              </li>
            `)}
          </ul>
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
