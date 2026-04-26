// XMclaw — Memory page
//
// Lists files in the memory store (auto-memory MD files). Backed by
// /api/v2/memory which returns a directory listing of the user's memory
// dir. Click a row to load + view content via /api/v2/memory/{name}.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";

export function MemoryPage({ token }) {
  const [files, setFiles] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);   // filename
  const [viewer, setViewer] = useState(null);       // {name, content} | "loading" | {error}

  useEffect(() => {
    let cancelled = false;
    apiGet("/api/v2/memory", token)
      .then((d) => {
        if (cancelled) return;
        const list = Array.isArray(d) ? d : (d && (d.files || d.entries || d.items)) || [];
        setFiles(list);
      })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [token]);

  const openFile = (name) => {
    setSelected(name);
    setViewer("loading");
    apiGet(`/api/v2/memory/${encodeURIComponent(name)}`, token)
      .then((d) => setViewer(d))
      .catch((e) => setViewer({ error: String(e.message || e) }));
  };

  if (error) return html`<section class="xmc-datapage"><h2>记忆</h2><p class="xmc-datapage__error">${error}</p></section>`;
  if (!files) return html`<section class="xmc-datapage"><p>加载中…</p></section>`;

  return html`
    <section class="xmc-datapage xmc-datapage--split" aria-labelledby="memory-title">
      <header class="xmc-datapage__header">
        <h2 id="memory-title">记忆</h2>
        <p class="xmc-datapage__subtitle">用户级记忆文件（${files.length}）。点击查看内容。</p>
      </header>
      <div class="xmc-datapage__split">
        <aside class="xmc-datapage__sidebar">
          ${files.length === 0
            ? html`<p class="xmc-datapage__empty">尚无记忆条目</p>`
            : html`
                <ul class="xmc-datapage__list">
                  ${files.map((f) => {
                    const name = typeof f === "string" ? f : (f.name || f.filename || f.path);
                    const size = f && (f.size != null) ? f.size : null;
                    const isActive = name === selected;
                    return html`
                      <li class="xmc-datapage__row xmc-datapage__row--clickable ${isActive ? "is-active" : ""}"
                          key=${name}
                          tabindex="0"
                          role="button"
                          onClick=${() => openFile(name)}
                          onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openFile(name); } }}>
                        <strong>${name}</strong>
                        ${size != null ? html`<small>${size} bytes</small>` : null}
                      </li>
                    `;
                  })}
                </ul>
              `}
        </aside>
        <article class="xmc-datapage__viewer">
          ${viewer === null ? html`<p class="xmc-datapage__hint">从左侧选一个文件查看。</p>` : null}
          ${viewer === "loading" ? html`<p>加载中…</p>` : null}
          ${viewer && viewer.error ? html`<p class="xmc-datapage__error">${viewer.error}</p>` : null}
          ${viewer && viewer.content != null ? html`
            <header class="xmc-datapage__viewer-header">
              <h3>${viewer.name}</h3>
            </header>
            <pre class="xmc-datapage__viewer-body">${viewer.content}</pre>
          ` : null}
        </article>
      </div>
    </section>
  `;
}
