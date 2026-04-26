// XMclaw — Memory page
//
// Lists files in the memory store (auto-memory MD files). Backed by
// /api/v2/memory which returns a directory listing of the user's memory
// dir. Click-to-view will land later — for now this is a discoverability
// surface, mirrors `xmclaw memory list` from the CLI.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";

export function MemoryPage({ token }) {
  const [files, setFiles] = useState(null);
  const [error, setError] = useState(null);

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

  if (error) return html`<section class="xmc-datapage"><h2>记忆</h2><p class="xmc-datapage__error">${error}</p></section>`;
  if (!files) return html`<section class="xmc-datapage"><p>加载中…</p></section>`;

  return html`
    <section class="xmc-datapage" aria-labelledby="memory-title">
      <header class="xmc-datapage__header">
        <h2 id="memory-title">记忆</h2>
        <p class="xmc-datapage__subtitle">用户级记忆文件（${files.length}）。</p>
      </header>
      ${files.length === 0
        ? html`<p class="xmc-datapage__empty">尚无记忆条目</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${files.map((f) => {
                const name = typeof f === "string" ? f : (f.name || f.filename || f.path);
                const size = f && (f.size != null) ? f.size : null;
                return html`
                  <li class="xmc-datapage__row" key=${name}>
                    <strong>${name}</strong>
                    ${size != null ? html`<small>${size} bytes</small>` : null}
                  </li>
                `;
              })}
            </ul>
          `}
    </section>
  `;
}
