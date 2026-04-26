// XMclaw — MessageBubble
//
// One row in the chat transcript. Renders user / assistant / system / tool
// content with a stable id, role-based visual treatment, and streaming
// indicator when status === "streaming".
//
// Markdown is rendered token-by-token via lib/markdown.js. The keyed map
// over `tokens` lets Preact's reconciler update only the trailing token
// when a new LLM_CHUNK arrives — no full bubble repaint, no cursor jump.
// This mirrors open-webui's `Markdown.svelte` + cline's `MarkdownBlock.tsx`
// memoization pattern.
//
// Tool cards are user-controllable `<details>` with a live shimmer on the
// summary line while running (open-webui `ToolCallDisplay.svelte:127-138`).
// Auto-collapsing on completion (the prior behavior) hid the result the
// moment it arrived — opposite of what the user wants.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

import { lex, renderTokenHtml } from "../../lib/markdown.js";
import { Spinner } from "../atoms/spinner.js";
import { Badge } from "../atoms/badge.js";

function ToolCard({ call }) {
  const tone =
    call.status === "ok" ? "success" : call.status === "error" ? "error" : "muted";
  const label =
    call.status === "ok" ? "ok" : call.status === "error" ? "error" : "running";
  const argsPreview = (() => {
    try {
      return JSON.stringify(call.args, null, 2);
    } catch (_) {
      return String(call.args);
    }
  })();
  // Open/close is user-controlled. Running state is conveyed by the spinner
  // + shimmer class on the summary; auto-collapse on completion would hide
  // the result the moment it arrived.
  return html`
    <details class="xmc-toolcard">
      <summary
        class=${"xmc-toolcard__summary" + (call.status === "running" ? " is-running" : "")}
      >
        <code class="xmc-toolcard__name">${call.name}</code>
        <${Badge} tone=${tone}>${label}</${Badge}>
        ${call.status === "running"
          ? html`<${Spinner} size="sm" label="running" />`
          : null}
      </summary>
      <div class="xmc-toolcard__body">
        <div class="xmc-toolcard__section">
          <div class="xmc-toolcard__label">args</div>
          <pre class="xmc-toolcard__pre">${argsPreview}</pre>
        </div>
        ${call.result != null
          ? html`
              <div class="xmc-toolcard__section">
                <div class="xmc-toolcard__label">result</div>
                <pre class="xmc-toolcard__pre">${call.result}</pre>
              </div>
            `
          : null}
      </div>
    </details>
  `;
}

function MarkdownBody({ content }) {
  // Lex once per render; lex itself memoises by source string identity, so
  // re-renders with the same content are O(1). When a new chunk arrives,
  // only the LAST token's html string changes, so Preact's keyed diff
  // touches a single child node — no flicker, no cursor jump.
  const tokens = lex(content || "");
  if (!tokens.length) {
    return html`<div class="xmc-msg__body xmc-md"></div>`;
  }
  return html`
    <div class="xmc-msg__body xmc-md">
      ${tokens.map(
        (tok) => html`
          <div
            key=${tok.idx}
            data-tok-type=${tok.type || "text"}
            dangerouslySetInnerHTML=${{ __html: renderTokenHtml(tok) }}
          ></div>
        `
      )}
    </div>
  `;
}

export function MessageBubble({ message }) {
  const role = message.role || "system";
  const isUser = role === "user";
  const isSystem = role === "system";
  const streaming = message.status === "streaming";
  const errored = message.status === "error";

  const cls =
    "xmc-msg xmc-msg--" +
    role +
    (streaming ? " is-streaming" : "") +
    (errored ? " is-error" : "");

  return html`
    <article
      class=${cls}
      data-msg-id=${message.id}
      data-role=${role}
      role=${isSystem ? "alert" : "article"}
      aria-busy=${streaming ? "true" : "false"}
    >
      <header class="xmc-msg__header">
        <span class="xmc-msg__role">${isUser ? "you" : isSystem ? "system" : "assistant"}</span>
        ${message.ultrathink
          ? html`<${Badge} tone="info">ultrathink</${Badge}>`
          : null}
        ${streaming ? html`<${Spinner} size="sm" label="streaming" />` : null}
      </header>
      <${MarkdownBody} content=${message.content || ""} />
      ${(message.toolCalls || []).map(
        (call) => html`<${ToolCard} key=${call.id} call=${call} />`
      )}
    </article>
  `;
}
