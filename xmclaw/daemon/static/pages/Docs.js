// XMclaw — DocsPage v2: local docs/ index + markdown render.
//
// We started with an iframe-to-GitHub /docs port (1:1 with Hermes
// DocsPage.tsx), but GitHub serves X-Frame-Options:DENY so the iframe
// loads blank. Solution: serve the same docs we have on disk via
// /api/v2/docs and render them with our marked.lexer pipeline.
//
// Layout:
//   Left rail: scrollable list of docs/*.md with title + filename.
//   Right pane: rendered markdown of selected doc + "open on GitHub"
//   external link.
//
// This is the layout Hermes Docs.tsx would have if their docs lived
// in the repo instead of behind a public URL.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { lex, renderTokenHtml } from "../lib/markdown.js";

function Icon({ d, className }) {
  return html`
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
         class=${"xmc-icon " + (className || "")} aria-hidden="true">
      <path d=${d} />
    </svg>
  `;
}

const I_EXTLINK  = "M15 3h6v6 M10 14 21 3 M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5";
const I_FILE     = "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6";

const GITHUB_BASE = "https://github.com/1593959/XMclaw/blob/main/docs/";

export function DocsPage({ token }) {
  const [docs, setDocs] = useState(null);
  const [error, setError] = useState(null);
  const [activePath, setActivePath] = useState(null);
  const [doc, setDoc] = useState(null);
  const [docLoading, setDocLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiGet("/api/v2/docs", token)
      .then((d) => {
        if (cancelled) return;
        const list = d.docs || [];
        setDocs(list);
        // Pick the most-likely entry-point doc by default.
        if (list.length) {
          const preferred = list.find((x) => /readme|overview|index|architecture/i.test(x.path))
                          || list[0];
          setActivePath(preferred.path);
        }
      })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [token]);

  useEffect(() => {
    if (!activePath) return;
    let cancelled = false;
    setDocLoading(true);
    apiGet("/api/v2/docs/" + encodeURIComponent(activePath), token)
      .then((d) => { if (!cancelled) setDoc(d); })
      .catch((e) => {
        if (!cancelled) setDoc({ error: String(e.message || e) });
      })
      .finally(() => { if (!cancelled) setDocLoading(false); });
    return () => { cancelled = true; };
  }, [activePath, token]);

  const tokens = (doc && doc.content) ? lex(doc.content) : null;
  const githubUrl = activePath ? GITHUB_BASE + activePath : null;

  if (error) {
    return html`
      <section class="xmc-h-page" aria-labelledby="docs-title">
        <header class="xmc-h-page__header">
          <h2 id="docs-title" class="xmc-h-page__title">文档</h2>
        </header>
        <div class="xmc-h-page__body"><div class="xmc-h-error">${error}</div></div>
      </section>
    `;
  }

  return html`
    <section class="xmc-h-page" aria-labelledby="docs-title">
      <header class="xmc-h-page__header">
        <div class="xmc-h-page__heading">
          <h2 id="docs-title" class="xmc-h-page__title">文档</h2>
          <p class="xmc-h-page__subtitle">
            从 daemon 的 <code>docs/</code> 直接读取 — 不走 iframe，
            支持完整 Markdown 渲染（GitHub 拒嵌 iframe 的兼容方案）。
          </p>
        </div>
        <div class="xmc-h-page__actions">
          ${githubUrl
            ? html`
              <a
                href=${githubUrl}
                target="_blank"
                rel="noopener noreferrer"
                class="xmc-h-btn"
              >
                <${Icon} d=${I_EXTLINK} />
                在 GitHub 查看
              </a>
            `
            : null}
        </div>
      </header>

      <div class="xmc-h-page__body xmc-h-docs2__body">
        <aside class="xmc-h-docs2__rail" aria-label="docs index">
          ${docs === null
            ? html`<div class="xmc-h-loading">载入中…</div>`
            : docs.length === 0
              ? html`<div class="xmc-h-empty">没有找到 docs/*.md</div>`
              : html`
                <ul class="xmc-h-docs2__list">
                  ${docs.map((d) => html`
                    <li key=${d.path}>
                      <button
                        type="button"
                        class=${"xmc-h-docs2__item " + (d.path === activePath ? "is-active" : "")}
                        onClick=${() => setActivePath(d.path)}
                      >
                        <${Icon} d=${I_FILE} className="xmc-h-docs2__item-icon" />
                        <span class="xmc-h-docs2__item-main">
                          <strong>${d.title}</strong>
                          <small>${d.path}</small>
                        </span>
                      </button>
                    </li>
                  `)}
                </ul>
              `}
        </aside>

        <article class="xmc-h-docs2__viewer">
          ${docLoading
            ? html`<div class="xmc-h-loading">载入中…</div>`
            : !doc
              ? html`<div class="xmc-h-empty">选择左侧文档</div>`
              : doc.error
                ? html`<div class="xmc-h-error">${doc.error}</div>`
                : html`
                  <div class="xmc-h-docs2__md xmc-h-msgbubble__body">
                    ${tokens.map((t) => html`
                      <div
                        key=${t.idx}
                        data-tok-type=${t.type || "text"}
                        dangerouslySetInnerHTML=${{ __html: renderTokenHtml(t) }}
                      ></div>
                    `)}
                  </div>
                `}
        </article>
      </div>
    </section>
  `;
}
