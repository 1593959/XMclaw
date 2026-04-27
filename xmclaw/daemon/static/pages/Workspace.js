// XMclaw — Workspace page
//
// Phase 1 redesign per docs/PRODUCT_REDESIGN.md §1.
//
// "Workspace" = a directory on disk. Mirrors Cline's `WorkspaceRoot`
// (`cline/src/core/workspace/WorkspaceRootManager.ts:11-42`) — it is NOT a
// file-tree editor for skills/agents/personas/memory. None of the peers do
// that, and conflating those concepts under one page is what produced the
// "一团糟" complaint.
//
// What this page now does:
//   - Show the current workspace root + the per-project block layout under
//     `<root>/.xmclaw/{agents,skills,rules,prompts,mcpServers,memory}` as
//     plain links into the matching sidebar pages
//   - Let the user paste a different absolute path to switch (browser
//     folder picker is Chromium-only via `showDirectoryPicker()`; manual
//     entry works everywhere)
//
// Phase 2 will wire `PUT /api/v2/workspace` so the daemon honors the new
// root for subsequent agent turns. For now we persist the user's choice in
// `localStorage.xmcWorkspaceRoot` so refresh remembers it; the daemon
// falls back to its config-supplied workspace until the API lands.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";

const LS_KEY = "xmcWorkspaceRoot";

function BlockLink({ icon, label, href, hint }) {
  return html`
    <a class="xmc-ws-block" href=${href}>
      <span class="xmc-ws-block__icon" aria-hidden="true">${icon}</span>
      <div>
        <strong class="xmc-ws-block__label">${label}</strong>
        <small class="xmc-ws-block__hint">${hint}</small>
      </div>
    </a>
  `;
}

export function WorkspacePage({ token }) {
  const [error, setError] = useState(null);
  const [roots, setRoots] = useState(null);
  // Workspace state persisted by daemon (~/.xmclaw/state.json).
  const [wsState, setWsState] = useState(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  // Compute "active" from daemon state when available, fall back to
  // localStorage for the brief window before the GET resolves.
  const activeRoot =
    wsState && wsState.roots && wsState.roots[wsState.primary_index || 0]?.path
      ? wsState.roots[wsState.primary_index || 0].path
      : (() => { try { return localStorage.getItem(LS_KEY) || ""; } catch (_) { return ""; } })();

  const loadAll = () => {
    apiGet("/api/v2/files/roots", token)
      .then((d) => setRoots(d.roots || []))
      .catch((e) => setError(String(e.message || e)));
    apiGet("/api/v2/workspace", token)
      .then((d) => setWsState(d))
      .catch((e) => setError(String(e.message || e)));
  };

  useEffect(loadAll, [token]);

  const onPickFolder = async () => {
    if (typeof window.showDirectoryPicker !== "function") {
      alert(
        "当前浏览器不支持文件夹 picker。请手动粘贴绝对路径。\n（仅 Chromium 系浏览器支持 showDirectoryPicker。）"
      );
      return;
    }
    try {
      const handle = await window.showDirectoryPicker({ mode: "read" });
      // Browsers only expose the basename for security, not the absolute
      // path — same constraint Cline hits when prompting users to paste a
      // workspace path on first use.
      setDraft(handle.name);
    } catch (e) {
      if (e?.name !== "AbortError") {
        console.warn("[xmc] folder picker rejected", e);
      }
    }
  };

  const onApply = async () => {
    const p = (draft || "").trim();
    if (!p) return;
    setBusy(true);
    try {
      const res = await fetch(
        "/api/v2/workspace" + (token ? `?token=${encodeURIComponent(token)}` : ""),
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ action: "add", path: p }),
        }
      );
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
      try { localStorage.setItem(LS_KEY, p); } catch (_) {}
      setWsState(data);
      setDraft("");
      toast.success(`工作区已添加并设为活动：${p}`);
    } catch (e) {
      toast.error("添加失败：" + (e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const onSetPrimary = async (index) => {
    setBusy(true);
    try {
      const res = await fetch(
        "/api/v2/workspace" + (token ? `?token=${encodeURIComponent(token)}` : ""),
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ action: "set_primary", index }),
        }
      );
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
      setWsState(data);
      toast.success("主工作区已切换");
    } catch (e) {
      toast.error("切换失败：" + (e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const onRemove = async (path) => {
    if (!confirm(`移除工作区:\n${path}？`)) return;
    setBusy(true);
    try {
      const res = await fetch(
        "/api/v2/workspace" + (token ? `?token=${encodeURIComponent(token)}` : ""),
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ action: "remove", path }),
        }
      );
      const data = await res.json();
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
      setWsState(data);
      toast.success("已移除");
    } catch (e) {
      toast.error("移除失败：" + (e.message || e));
    } finally {
      setBusy(false);
    }
  };

  if (error) {
    return html`
      <section class="xmc-datapage" aria-labelledby="ws-title">
        <header class="xmc-datapage__header">
          <h2 id="ws-title">工作区</h2>
        </header>
        <p class="xmc-datapage__error">${error}</p>
      </section>
    `;
  }

  return html`
    <section class="xmc-datapage" aria-labelledby="ws-title">
      <header class="xmc-datapage__header">
        <h2 id="ws-title">工作区</h2>
        <p class="xmc-datapage__subtitle">
          工作区 = 一个项目目录（mirrors Cline <code>WorkspaceRoot</code>）。每项目的智能体 / 规则 /
          技能 / 记忆 配置约定在
          <code>&lt;workspace&gt;/.xmclaw/</code>，全局配置在
          <code>~/.xmclaw/</code>。Phase 2 接 daemon API；当前为只读视图 + 浏览器端记忆。
        </p>
      </header>

      <div class="xmc-datapage__row" style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap">
        <strong style="flex:0 0 auto">当前工作区：</strong>
        <code style="flex:1 1 320px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${activeRoot || "(未设置 — daemon 用其默认 cwd)"}</code>
      </div>

      <div class="xmc-datapage__row" style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-top:.75rem">
        <input
          type="text"
          placeholder="粘贴绝对路径，例如 C:\\code\\my-project 或 /home/me/code"
          value=${draft}
          onInput=${(e) => setDraft(e.target.value)}
          onKeyDown=${(e) => {
            if (e.key === "Enter") onApply();
          }}
          style="flex:1 1 320px;min-width:0;font-family:var(--xmc-font-mono);font-size:var(--xmc-font-size-sm);padding:.4rem .6rem"
        />
        <button type="button" onClick=${onPickFolder} disabled=${busy}>选择文件夹…</button>
        <button type="button" onClick=${onApply} disabled=${busy || !draft.trim()}>添加并设为活动</button>
      </div>

      ${wsState && wsState.roots && wsState.roots.length
        ? html`
          <h3 style="margin:1.5rem 0 .5rem">已注册工作区（daemon 持久化在 ~/.xmclaw/state.json）</h3>
          <ul class="xmc-datapage__list">
            ${wsState.roots.map((r, i) => {
              const isPrimary = (wsState.primary_index || 0) === i;
              return html`
                <li class="xmc-datapage__row" key=${r.path} style="display:flex;align-items:center;gap:.5rem">
                  <strong style="flex:0 0 auto;font-size:.95rem">${r.name}</strong>
                  ${isPrimary ? html`<span class="xmc-h-badge xmc-h-badge--success">活动</span>` : null}
                  ${r.vcs === "git" ? html`<span class="xmc-h-badge">git</span>` : null}
                  <code style="flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.path}</code>
                  ${!isPrimary
                    ? html`<button type="button" onClick=${() => onSetPrimary(i)} disabled=${busy} title="设为活动">设为活动</button>`
                    : null}
                  <button
                    type="button"
                    onClick=${() => onRemove(r.path)}
                    disabled=${busy}
                    title="移除"
                    class="xmc-h-btn--danger"
                    style="background:color-mix(in srgb,var(--color-destructive) 14%,transparent);color:var(--color-destructive)"
                  >移除</button>
                </li>
              `;
            })}
          </ul>
        `
        : null}

      <h3 style="margin:1.5rem 0 .5rem">这个工作区的配置块</h3>
      <p class="xmc-datapage__subtitle" style="margin-bottom:.75rem">
        每一类是一个独立 sidebar 页（抄 Continue 的目录约定 — agents / rules / models / prompts /
        mcpServers / skills）。点击进入对应页编辑文件。
      </p>

      <div style="display:grid;gap:.5rem;grid-template-columns:repeat(auto-fill,minmax(220px,1fr))">
        <${BlockLink} icon="🤖" label="智能体" href="/agents" hint="<root>/.xmclaw/agents/*.yaml" />
        <${BlockLink} icon="📚" label="技能" href="/skills" hint="<root>/.xmclaw/skills/<name>/SKILL.md" />
        <${BlockLink} icon="🧠" label="记忆" href="/memory" hint="<root>/.xmclaw/memory/MEMORY.md" />
        <${BlockLink} icon="🧰" label="工具" href="/tools" hint="builtin + MCP servers" />
        <${BlockLink} icon="🔒" label="安全" href="/security" hint="approval policy + injection log" />
      </div>

      <h3 style="margin:1.5rem 0 .5rem">XMclaw 数据根目录</h3>
      <p class="xmc-datapage__subtitle">
        Daemon 自身的状态、密钥、事件日志在 <code>~/.xmclaw/v2/</code>。下面是 daemon 报告的几个固定根。
      </p>
      ${roots == null
        ? html`<p>加载中…</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${roots.map(
                (r) => html`
                  <li class="xmc-datapage__row" key=${r.path}>
                    <strong>${r.label}</strong>
                    <code style="margin-left:.5rem">${r.path}</code>
                    ${r.exists ? null : html`<small style="margin-left:.5rem;color:var(--xmc-fg-muted)">(不存在)</small>`}
                  </li>
                `
              )}
            </ul>
          `}
    </section>
  `;
}
