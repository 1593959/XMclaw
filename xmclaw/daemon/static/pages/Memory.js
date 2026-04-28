// XMclaw — Memory page
//
// Three tabs:
//   1. 标识 — edit the 7 canonical persona files (SOUL/AGENTS/USER/MEMORY/
//      IDENTITY/TOOLS/BOOTSTRAP). Backed by GET/PUT /api/v2/profiles/active.
//      Saves rebuild app.state.agent's system prompt so edits land on the
//      next turn — no daemon restart needed.
//   2. 笔记 — the legacy memory notes browser (GET /api/v2/memory + per-file
//      GET/POST). Lets the user keep arbitrary topic notes alongside the
//      structured persona files. POST upserts on save, so notes are now
//      editable too (the prior version was read-only).
//   3. 日记 — daily journal (GET /api/v2/journal + per-date GET/PUT).
//      "Today" loads on tab open; the date list shows past entries with
//      previews. Empty save deletes the file.

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback, useMemo } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";

// ── shared ────────────────────────────────────────────────────────────

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

function todayIso() {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

const TAB_LABELS = [
  { id: "identity", label: "标识", hint: "SOUL / AGENTS / USER / MEMORY 等核心人格文件" },
  { id: "notes", label: "笔记", hint: "随手保存的主题笔记（~/.xmclaw/memory/*.md）" },
  { id: "journal", label: "日记", hint: "按日期归档的对话/事件记录" },
];

// ── 标识 (Identity) tab ───────────────────────────────────────────────

function IdentityTab({ token }) {
  const [state, setState] = useState({ status: "loading", data: null, error: null });
  const [active, setActive] = useState(null);     // basename
  const [draft, setDraft] = useState("");         // edit buffer
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
      // Refetch so layer/exists badges update.
      load();
    } catch (e) {
      toast.error("保存失败：" + (e.message || e));
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
      <p class="xmc-datapage__subtitle" style="margin:.4rem 0 .8rem">
        这 7 个文件是 agent 的"灵魂"。每次对话开始时会被注入 system prompt（顺序固定，按 OpenClaw 约定）。改完保存即生效，无需重启 daemon。
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

// ── 笔记 (Notes) tab ──────────────────────────────────────────────────

function NotesTab({ token }) {
  const [files, setFiles] = useState(null);
  const [error, setError] = useState(null);
  const [active, setActive] = useState(null);
  const [draft, setDraft] = useState("");
  const [pristine, setPristine] = useState("");
  const [busy, setBusy] = useState(false);
  const [newName, setNewName] = useState("");

  const load = useCallback(() => {
    apiGet("/api/v2/memory", token)
      .then((d) => {
        const list = Array.isArray(d) ? d : (d && (d.files || d.entries || d.items)) || [];
        setFiles(list);
      })
      .catch((e) => setError(String(e.message || e)));
  }, [token]);

  useEffect(load, [load]);

  const open = (name) => {
    setActive(name);
    apiGet(`/api/v2/memory/${encodeURIComponent(name)}`, token)
      .then((d) => {
        setDraft(d.content || "");
        setPristine(d.content || "");
      })
      .catch((e) => toast.error(e.message || String(e)));
  };

  const onSave = async () => {
    if (!active) return;
    setBusy(true);
    try {
      await apiPost(`/api/v2/memory/${encodeURIComponent(active)}`, token, { content: draft });
      setPristine(draft);
      toast.success(`已保存 ${active}`);
      load();
    } catch (e) {
      toast.error("保存失败：" + (e.message || e));
    } finally {
      setBusy(false);
    }
  };

  const onCreate = async () => {
    let name = (newName || "").trim();
    if (!name) return;
    if (!name.endsWith(".md")) name += ".md";
    setBusy(true);
    try {
      await apiPost(`/api/v2/memory/${encodeURIComponent(name)}`, token, { content: `# ${name.replace(/\.md$/, "")}\n\n` });
      toast.success(`已创建 ${name}`);
      setNewName("");
      load();
      setTimeout(() => open(name), 100);
    } catch (e) {
      toast.error("创建失败：" + (e.message || e));
    } finally {
      setBusy(false);
    }
  };

  if (error) return html`<p class="xmc-datapage__error">${error}</p>`;
  if (!files) return html`<p class="xmc-datapage__hint">加载中…</p>`;
  const dirty = active && draft !== pristine;

  return html`
    <div>
      <div class="xmc-datapage__row" style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-bottom:.6rem">
        <input
          type="text"
          placeholder="新笔记名（自动追加 .md）"
          value=${newName}
          onInput=${(e) => setNewName(e.target.value)}
          onKeyDown=${(e) => { if (e.key === "Enter") onCreate(); }}
          style="flex:1 1 220px;min-width:0;padding:.4rem .6rem;font-family:var(--xmc-font-mono);font-size:.85rem"
        />
        <button type="button" class="xmc-h-btn" onClick=${onCreate} disabled=${busy || !newName.trim()}>新建笔记</button>
      </div>
      <div class="xmc-datapage__split">
        <aside class="xmc-datapage__sidebar">
          ${files.length === 0
            ? html`<p class="xmc-datapage__empty">尚无笔记</p>`
            : html`
                <ul class="xmc-datapage__list">
                  ${files.map((f) => {
                    const name = typeof f === "string" ? f : (f.name || f.filename || f.path);
                    const size = f && f.size != null ? f.size : null;
                    const isActive = name === active;
                    return html`
                      <li
                        class="xmc-datapage__row xmc-datapage__row--clickable ${isActive ? "is-active" : ""}"
                        key=${name}
                        tabindex="0"
                        role="button"
                        onClick=${() => open(name)}
                        onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(name); } }}
                      >
                        <strong>${name}</strong>
                        ${size != null ? html`<small>${size}B</small>` : null}
                      </li>
                    `;
                  })}
                </ul>
              `}
        </aside>
        <article class="xmc-datapage__viewer" style="display:flex;flex-direction:column;min-height:0">
          ${active
            ? html`
              <header class="xmc-datapage__viewer-header">
                <h3 style="margin:0">${active}</h3>
              </header>
              <textarea
                value=${draft}
                onInput=${(e) => setDraft(e.target.value)}
                spellcheck="false"
                style="flex:1 1 auto;min-height:320px;width:100%;font-family:var(--xmc-font-mono);font-size:.85rem;padding:.6rem;border:1px solid var(--color-border);border-radius:6px;background:var(--color-card);color:var(--color-fg);resize:vertical;line-height:1.5"
              ></textarea>
              <div style="display:flex;gap:.5rem;align-items:center;margin-top:.5rem">
                <button type="button" class="xmc-h-btn xmc-h-btn--primary" onClick=${onSave} disabled=${busy || !dirty}>
                  ${busy ? "保存中…" : dirty ? "保存" : "已保存"}
                </button>
                ${dirty
                  ? html`<button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${() => setDraft(pristine)} disabled=${busy}>放弃修改</button>`
                  : null}
                <small class="xmc-datapage__subtitle" style="margin-left:auto">${draft.length} 字符</small>
              </div>
            `
            : html`<p class="xmc-datapage__hint">从左侧选一篇笔记，或上面新建。</p>`}
        </article>
      </div>
    </div>
  `;
}

// ── 日记 (Journal) tab ────────────────────────────────────────────────

function JournalTab({ token }) {
  const [entries, setEntries] = useState(null);
  const [error, setError] = useState(null);
  const [activeDate, setActiveDate] = useState(todayIso());
  const [draft, setDraft] = useState("");
  const [pristine, setPristine] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    apiGet("/api/v2/journal", token)
      .then((d) => setEntries(d.entries || []))
      .catch((e) => setError(String(e.message || e)));
  }, [token]);

  const openDate = useCallback((date) => {
    setActiveDate(date);
    apiGet(`/api/v2/journal/${encodeURIComponent(date)}`, token)
      .then((d) => {
        setDraft(d.content || "");
        setPristine(d.content || "");
      })
      .catch((e) => toast.error(e.message || String(e)));
  }, [token]);

  useEffect(() => {
    load();
    openDate(todayIso());
  }, [load, openDate]);

  const onSave = async () => {
    setBusy(true);
    try {
      await apiPut(`/api/v2/journal/${encodeURIComponent(activeDate)}`, token, { content: draft });
      setPristine(draft);
      toast.success(`已保存 ${activeDate} 日记`);
      load();
    } catch (e) {
      toast.error("保存失败：" + (e.message || e));
    } finally {
      setBusy(false);
    }
  };

  if (error) return html`<p class="xmc-datapage__error">${error}</p>`;
  const dirty = draft !== pristine;
  const today = todayIso();

  return html`
    <div>
      <div class="xmc-datapage__row" style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-bottom:.6rem">
        <strong>查看日期：</strong>
        <input
          type="date"
          value=${activeDate}
          onChange=${(e) => openDate(e.target.value)}
          max=${today}
          style="font-family:var(--xmc-font-mono);font-size:.85rem;padding:.3rem .5rem"
        />
        <button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${() => openDate(today)} disabled=${activeDate === today}>跳到今天</button>
        ${activeDate === today ? html`<span class="xmc-h-badge xmc-h-badge--info">今天</span>` : null}
      </div>
      <div class="xmc-datapage__split">
        <aside class="xmc-datapage__sidebar">
          ${entries == null
            ? html`<p class="xmc-datapage__hint">加载中…</p>`
            : entries.length === 0
              ? html`<p class="xmc-datapage__empty">尚无日记 — 在右侧写下今天的事</p>`
              : html`
                  <ul class="xmc-datapage__list">
                    ${entries.map((e) => {
                      const isActive = e.date === activeDate;
                      return html`
                        <li
                          class="xmc-datapage__row xmc-datapage__row--clickable ${isActive ? "is-active" : ""}"
                          key=${e.date}
                          tabindex="0"
                          role="button"
                          onClick=${() => openDate(e.date)}
                          onKeyDown=${(ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openDate(e.date); } }}
                        >
                          <strong>${e.date}</strong>
                          ${e.preview ? html`<small style="display:block;opacity:.7;font-size:.75rem">${e.preview}</small>` : null}
                        </li>
                      `;
                    })}
                  </ul>
                `}
        </aside>
        <article class="xmc-datapage__viewer" style="display:flex;flex-direction:column;min-height:0">
          <header class="xmc-datapage__viewer-header">
            <h3 style="margin:0">${activeDate}${activeDate === today ? "（今天）" : ""}</h3>
          </header>
          <textarea
            value=${draft}
            placeholder=${`# ${activeDate}\n\n今天发生了什么…`}
            onInput=${(e) => setDraft(e.target.value)}
            spellcheck="false"
            style="flex:1 1 auto;min-height:320px;width:100%;font-family:var(--xmc-font-mono);font-size:.85rem;padding:.6rem;border:1px solid var(--color-border);border-radius:6px;background:var(--color-card);color:var(--color-fg);resize:vertical;line-height:1.5"
          ></textarea>
          <div style="display:flex;gap:.5rem;align-items:center;margin-top:.5rem">
            <button type="button" class="xmc-h-btn xmc-h-btn--primary" onClick=${onSave} disabled=${busy || !dirty}>
              ${busy ? "保存中…" : dirty ? "保存" : "已保存"}
            </button>
            ${dirty
              ? html`<button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${() => setDraft(pristine)} disabled=${busy}>放弃修改</button>`
              : null}
            <small class="xmc-datapage__subtitle" style="margin-left:auto">${draft.length} 字符 · 空内容会删除文件</small>
          </div>
        </article>
      </div>
    </div>
  `;
}

// ── shell ─────────────────────────────────────────────────────────────

export function MemoryPage({ token }) {
  const [tab, setTab] = useState("identity");
  const activeMeta = useMemo(() => TAB_LABELS.find((t) => t.id === tab), [tab]);

  return html`
    <section class="xmc-datapage" aria-labelledby="memory-title">
      <header class="xmc-datapage__header">
        <h2 id="memory-title">记忆</h2>
        <p class="xmc-datapage__subtitle">${activeMeta ? activeMeta.hint : ""}</p>
      </header>
      <nav class="xmc-mem-tabs" role="tablist" aria-label="记忆类别" style="display:flex;gap:.4rem;border-bottom:1px solid var(--color-border);margin-bottom:.8rem;flex-wrap:wrap">
        ${TAB_LABELS.map((t) => {
          const isActive = t.id === tab;
          return html`
            <button
              type="button"
              role="tab"
              aria-selected=${isActive}
              onClick=${() => setTab(t.id)}
              key=${t.id}
              style=${`appearance:none;background:none;border:none;padding:.5rem .9rem;font:inherit;cursor:pointer;color:${isActive ? "var(--color-primary)" : "var(--xmc-fg-muted)"};border-bottom:2px solid ${isActive ? "var(--color-primary)" : "transparent"};font-weight:${isActive ? "600" : "500"}`}
            >
              ${t.label}
            </button>
          `;
        })}
      </nav>
      ${tab === "identity" ? html`<${IdentityTab} token=${token} />` : null}
      ${tab === "notes" ? html`<${NotesTab} token=${token} />` : null}
      ${tab === "journal" ? html`<${JournalTab} token=${token} />` : null}
    </section>
  `;
}
