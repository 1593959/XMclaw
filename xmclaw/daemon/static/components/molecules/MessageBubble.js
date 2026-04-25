// XMclaw — MessageBubble
//
// One row in the chat transcript. Renders user / assistant / system / tool
// content with a stable id, role-based visual treatment, and streaming
// indicator when status === "streaming".
//
// Markdown is rendered via the in-house tiny renderer (lib/markdown.js).
// dangerouslySetInnerHTML is safe here because the renderer escapes all
// untrusted text before splicing fenced blocks back in.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

import { renderMarkdown } from "../../lib/markdown.js";
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
  return html`
    <details class="xmc-toolcard" open=${call.status === "running"}>
      <summary class="xmc-toolcard__summary">
        <code class="xmc-toolcard__name">${call.name}</code>
        <${Badge} tone=${tone}>${label}</${Badge}>
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

export function MessageBubble({ message }) {
  const role = message.role || "system";
  const isUser = role === "user";
  const isSystem = role === "system";
  const streaming = message.status === "streaming";
  const errored = message.status === "error";
  const renderedHtml = renderMarkdown(message.content || "");

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
      <div
        class="xmc-msg__body xmc-md"
        dangerouslySetInnerHTML=${{ __html: renderedHtml }}
      ></div>
      ${(message.toolCalls || []).map(
        (call) => html`<${ToolCard} key=${call.id} call=${call} />`
      )}
    </article>
  `;
}
