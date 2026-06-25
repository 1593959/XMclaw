import DOMPurify from "dompurify";

const ARTIFACT_FORBID_TAGS = [
  "base",
  "button",
  "embed",
  "form",
  "iframe",
  "input",
  "link",
  "meta",
  "object",
  "script",
  "select",
  "textarea",
];

const ARTIFACT_FORBID_ATTR = [
  "autofocus",
  "formaction",
  "integrity",
  "nonce",
  "ping",
  "srcdoc",
];

const URI_ATTRS = new Set([
  "action",
  "background",
  "cite",
  "data",
  "formaction",
  "href",
  "poster",
  "src",
  "xlink:href",
]);

let hooksInstalled = false;

function installHooks() {
  if (hooksInstalled) return;
  hooksInstalled = true;
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (!(node instanceof Element)) return;
    for (const attr of Array.from(node.attributes)) {
      const name = attr.name.toLowerCase();
      if (name.startsWith("on")) {
        node.removeAttribute(attr.name);
        continue;
      }
      if (URI_ATTRS.has(name) && !isSafeArtifactUrl(attr.value)) {
        node.removeAttribute(attr.name);
      }
    }
  });
}

function looksRelative(url: string): boolean {
  return (
    url.startsWith("/") ||
    url.startsWith("./") ||
    url.startsWith("../") ||
    (!url.includes(":") && !url.startsWith("//"))
  );
}

export function isSafeArtifactUrl(value: string): boolean {
  const url = value.trim();
  if (!url || url.startsWith("#")) return true;
  const lower = url.toLowerCase();
  if (lower.startsWith("data:image/")) return true;
  if (looksRelative(url)) return true;
  return false;
}

export function isSafeMarkdownHref(value: string): boolean {
  const url = value.trim();
  if (!url || url.startsWith("#") || looksRelative(url)) return true;
  try {
    const parsed = new URL(url);
    return ["http:", "https:", "mailto:", "tel:"].includes(parsed.protocol);
  } catch {
    return false;
  }
}

export function isSafeMarkdownImageUrl(value: string): boolean {
  const url = value.trim();
  if (!url) return false;
  const lower = url.toLowerCase();
  if (lower.startsWith("data:image/")) return true;
  return looksRelative(url);
}

export function sanitizeArtifactMarkup(markup: string): string {
  installHooks();
  return DOMPurify.sanitize(markup, {
    USE_PROFILES: { html: true, svg: true, svgFilters: true },
    FORBID_ATTR: ARTIFACT_FORBID_ATTR,
    FORBID_TAGS: ARTIFACT_FORBID_TAGS,
    SANITIZE_DOM: true,
    SANITIZE_NAMED_PROPS: true,
  });
}

export function artifactSrcDoc(markup: string): string {
  const safe = sanitizeArtifactMarkup(markup);
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; font-src data:; connect-src 'none'; script-src 'none'; base-uri 'none'; form-action 'none'"><style>html,body{margin:0;background:#fff;color:#111}img,svg{max-width:100%;height:auto}</style></head><body>${safe}</body></html>`;
}
