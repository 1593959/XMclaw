// XMclaw — Memory page Identity tab
//
// Split out of pages/Memory.js in B-52 to keep the parent file under
// the 500-line UI scaffold budget. Edits the 7 canonical persona
// files (SOUL/AGENTS/IDENTITY/USER/TOOLS/BOOTSTRAP/MEMORY) with an
// agent-write-count badge so the user can see which files the agent
// has been touching on its own.

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";
import { confirmDialog } from "../lib/dialog.js";

async function apiPut(path, token, body) {
  const url = path + (token ? `?token=${encodeURIComponent(token)}` : "");
  const res = await fetch(url, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

async function apiPost(path, token, body) {
  const url = path + (token ? `?token=${encodeURIComponent(token)}` : "");
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

export function IdentityTab({ token }) {
  const [state, setState] = useState({ status: "loading", data: null, error: null });
  const [active, setActive] = useState(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  // Per-file count of agent-driven writes (from .agent_writes.jsonl
  // sidecar). Powers the "agent" badge in the file rail showing which
  // files the agent has been editing on its own.
  const [agentWrites, setAgentWrites] = useState({});

  const load = useCallback(() => {
    setState({ status: "loading", data: null, error: null });
    apiGet("/api/v2/profiles/active", token)
      .then((d) => {
        setState({ status: "ready", data: d, error: null });
        if (d.files && d.files.length) {
          const first = d.files[0];
          setActive(first.basename);
          setDraft(first.content || "");
        }
      })
      .catch((e) => setState({ status: "error", data: null, error: String(e.message || e) }));
    apiGet("/api/v2/profiles/active/agent_writes", token)
      .then((d) => {
        const counts = {};
        for (const w of d.writes || []) {
          if (!w.file) continue;
          counts[w.file] = (counts[w.file] || 0) + 1;
        }
        setAgentWrites(counts);
      })
      .catch(() => setAgentWrites({}));
  }, [token]);

  useEffect(load, [load]);

  const onSelect = (basename) => {
    if (!state.data) return;
    const f = state.data.files.find((x) => x.basename === basename);
    if (!f) return;
    setActive(basename);
    setDraft(f.content || "");
  };

  const onSave = async () => {
    if (!active) return;
    setBusy(true);
    try {
      await apiPut(`/api/v2/profiles/active/${encodeURIComponent(active)}`, token, {
        content: draft,
      });
      toast.success(`已保存 ${active} — 下一轮对话生效`);
      load();
    } catch (e) {
      toast.error("保存失败：" + (e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const onDedupe = async () => {
    const ok = await confirmDialog({
      title: "整理重复条目",
      body: "合并同一事实的多次写入，保留最早日期。\n该操作不可撤销但只删重复，不删唯一内容。",
      confirmLabel: "整理",
    });
    if (!ok) return;
    setBusy(true);
    try {
      const r = await apiPost("/api/v2/profiles/active/dedupe", token, {});
      const removed = (r.files || []).reduce((acc, f) => acc + (f.removed_lines || 0), 0);
      toast.success(`整理完成 — 删除 ${removed} 行重复内容`);
      load();
      if (active && state.data) {
        const f = state.data.files.find((x) => x.basename === active);
        if (f) setDraft(f.content || "");
      }
    } catch (e) {
      toast.error("整理失败：" + (e.message || e));
    } finally {
      setBusy(false);
    }
  };

  if (state.status === "loading") {
    return html`<p class="xmc-datapage__hint">加载中…</p>`;
  }
  if (state.status === "error") {
    return html`<p class="xmc-datapage__error">${state.error}</p>`;
  }
  const data = state.data;
  const activeFile = data.files.find((f) => f.basename === active);
  const dirty = activeFile && draft !== (activeFile.content || "");

  const layerLabel = (layer) => {
    if (layer === "project") return "项目覆写";
    if (layer === "profile") return "用户档案";
    if (layer === "builtin") return "内置默认";
    return "未创建";
  };
  const layerTone = (layer) => {
    if (layer === "project") return "info";
    if (layer === "profile") return "success";
    if (layer === "builtin") return "muted";
    return "warn";
  };

  return html`
    <div class="xmc-mem-id">
      <header class="xmc-datapage__row" style="display:flex;gap:.5rem;align-items:baseline;flex-wrap:wrap">
        <strong>当前档案：</strong>
        <code>${data.profile_id}</code>
        <small class="xmc-datapage__subtitle" style="margin-left:.5rem">${data.profile_dir}</small>
      </header>
      <p class="xmc-datapage__subtitle" style="margin:.4rem 0 .8rem;display:flex;justify-content:space-between;align-items:center;gap:.5rem;flex-wrap:wrap">
        <span>这 7 个文件是 agent 的"灵魂"。每次对话开始时会被注入 system prompt。改完保存即生效，无需重启 daemon。</span>
        <button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${onDedupe} disabled=${busy} title="合并语义重复的条目（保留最早日期）" style="flex:0 0 auto">整理重复</button>
      </p>
      <div class="xmc-datapage__split">
        <aside class="xmc-datapage__sidebar">
          <ul class="xmc-datapage__list">
            ${data.files.map((f) => {
              const isActive = f.basename === active;
              const tone = layerTone(f.layer);
              const writes = agentWrites[f.basename] || 0;
              return html`
                <li
                  class="xmc-datapage__row xmc-datapage__row--clickable ${isActive ? "is-active" : ""}"
                  key=${f.basename}
                  tabindex="0"
                  role="button"
                  onClick=${() => onSelect(f.basename)}
                  onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(f.basename); } }}
                  style="display:flex;align-items:center;gap:.4rem;justify-content:space-between;flex-wrap:wrap"
                >
                  <strong style="font-size:.9rem">${f.basename}</strong>
                  <span style="display:flex;gap:.3rem;align-items:center">
                    ${writes > 0
                      ? html`<span class="xmc-h-badge xmc-h-badge--info" style="font-size:.6rem" title=${`agent 写入了 ${writes} 次`}>🤖 ${writes}</span>`
                      : null}
                    <span class="xmc-h-badge xmc-h-badge--${tone}" style="font-size:.6rem">${layerLabel(f.layer)}</span>
                  </span>
                </li>
              `;
            })}
          </ul>
        </aside>
        <article class="xmc-datapage__viewer" style="display:flex;flex-direction:column;min-height:0">
          ${activeFile
            ? html`
              <header class="xmc-datapage__viewer-header" style="display:flex;justify-content:space-between;align-items:baseline;gap:.5rem;flex-wrap:wrap">
                <h3 style="margin:0">${activeFile.basename}</h3>
                <small class="xmc-datapage__subtitle">
                  来源：<code>${activeFile.source}</code> · ${layerLabel(activeFile.layer)}
                </small>
              </header>
              <textarea
                class="xmc-mem-id__editor"
                value=${draft}
                onInput=${(e) => setDraft(e.target.value)}
                spellcheck="false"
                style="flex:1 1 auto;min-height:320px;width:100%;font-family:var(--xmc-font-mono);font-size:.85rem;padding:.6rem;border:1px solid var(--color-border);border-radius:6px;background:var(--color-card);color:var(--color-fg);resize:vertical;line-height:1.5"
              ></textarea>
              <div style="display:flex;gap:.5rem;align-items:center;margin-top:.5rem">
                <button type="button" class="xmc-h-btn xmc-h-btn--primary" onClick=${onSave} disabled=${busy || !dirty}>
                  ${busy ? "保存中…" : dirty ? "保存（下一轮生效）" : "已保存"}
                </button>
                ${dirty
                  ? html`<button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${() => setDraft(activeFile.content || "")} disabled=${busy}>放弃修改</button>`
                  : null}
                <small class="xmc-datapage__subtitle" style="margin-left:auto">${draft.length} 字符 · ${new Blob([draft]).size} 字节</small>
              </div>
            `
            : html`<p class="xmc-datapage__hint">从左侧选一个文件编辑</p>`}
        </article>
      </div>
    </div>
  `;
}
