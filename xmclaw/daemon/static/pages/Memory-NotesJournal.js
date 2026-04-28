// XMclaw — Memory page sub-tabs (Notes + Journal)
//
// Split out of pages/Memory.js in B-49 to keep the parent file under
// the 500-line UI scaffold budget. Both tabs are file-list + editor
// pairs, share the apiGet/apiPost/apiPut helpers re-exported here
// (their copies in Memory.js stay private to that file).

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";

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

// ── 笔记 (Notes) tab ──────────────────────────────────────────────────

export function NotesTab({ token }) {
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

export function JournalTab({ token }) {
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
