// XMclaw Agent Compact Message List — reference-only transcript
//
// Only shows the last N messages (configurable, default 3) in a compact
// single-line format. The operational detail lives in Plan/Tool/Thinking
// panels. The transcript is a reference — not the primary interface.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

function truncText(text, maxLen = 120) {
  if (!text) return "";
  const s = typeof text === "string" ? text : String(text);
  if (s.length <= maxLen) return s;
  return s.slice(0, maxLen) + "…";
}

export function CompactMessageList({
  messages = [],
  maxVisible = 3,
}) {
  if (!messages || messages.length === 0) {
    return html`<div class="compact-chat compact-chat--empty">
      <span class="compact-chat__hint">Start a new conversation…</span>
    </div>`;
  }

  const visible = messages.slice(-maxVisible);
  // Count how many were hidden
  const hidden = messages.length - visible.length;

  return html`
    <div class="compact-chat" role="log" aria-label="Conversation transcript">
      ${hidden > 0 ? html`
        <div class="compact-chat__more">
          ↑ ${hidden} earlier messages
        </div>
      ` : null}
      ${visible.map(msg => html`
        <div key=${msg.id} class=${"compact-msg compact-msg--" + (msg.role || "system")}>
          <span class="compact-msg__role">${msg.role === "user" ? "You" : msg.role === "assistant" ? "XM" : ""}</span>
          <span class="compact-msg__content">${truncText(msg.content)}</span>
        </div>
      `)}
    </div>
  `;
}
