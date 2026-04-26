// XMclaw — DocsPage 1:1 layout port of hermes-agent DocsPage.tsx
//
// Hermes embeds hermes-agent.nousresearch.com/docs in a sandboxed
// iframe with an external-link button in page-actions. We point the
// iframe at our own docs (configurable via XMC_DOCS_URL meta tag or
// localStorage). Default falls back to the GitHub /docs tree.

const { h } = window.__xmc.preact;
const { useMemo } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

function Icon({ d, className }) {
  return html`
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
         class=${"xmc-icon " + (className || "")} aria-hidden="true">
      <path d=${d} />
    </svg>
  `;
}

const I_EXTLINK = "M15 3h6v6 M10 14 21 3 M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5";

function resolveDocsUrl() {
  // 1. localStorage override
  try {
    const v = localStorage.getItem("xmc_docs_url");
    if (v && /^https?:/.test(v)) return v;
  } catch (_) {}
  // 2. <meta name="xmc-docs-url" content="..." />
  const m = document.querySelector('meta[name="xmc-docs-url"]');
  if (m && /^https?:/.test(m.content)) return m.content;
  // 3. Default — GitHub docs tree
  return "https://github.com/1593959/XMclaw/tree/main/docs";
}

export function DocsPage() {
  const docsUrl = useMemo(resolveDocsUrl, []);
  return html`
    <section class="xmc-h-page xmc-h-page--full" aria-labelledby="docs-title">
      <header class="xmc-h-page__header">
        <div class="xmc-h-page__heading">
          <h2 id="docs-title" class="xmc-h-page__title">文档</h2>
          <p class="xmc-h-page__subtitle">
            内嵌 XMclaw 文档（默认指向 GitHub <code>/docs</code>）。
            通过 <code>localStorage.xmc_docs_url</code> 或
            <code>&lt;meta name="xmc-docs-url"&gt;</code> 切换。
          </p>
        </div>
        <div class="xmc-h-page__actions">
          <a
            href=${docsUrl}
            target="_blank"
            rel="noopener noreferrer"
            class="xmc-h-btn"
          >
            <${Icon} d=${I_EXTLINK} />
            打开文档
          </a>
        </div>
      </header>

      <div class="xmc-h-page__body xmc-h-page__body--flush xmc-h-docs__body">
        <iframe
          title="XMclaw 文档"
          class="xmc-h-docs__iframe"
          src=${docsUrl}
          sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
          referrerpolicy="no-referrer-when-downgrade"
        ></iframe>
      </div>
    </section>
  `;
}
