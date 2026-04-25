// XMclaw — Chat page
//
// Composes MessageList + Composer + a small session header. Pure render —
// all WS / store wiring happens in app.js so this file stays trivially
// reusable from a future "session split" view.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

import { MessageList } from "../components/molecules/MessageList.js";
import { Composer } from "../components/molecules/Composer.js";
import { Badge } from "../components/atoms/badge.js";

export function ChatPage({ chat, session, connection, onSend, onChangeDraft, onTogglePlan, onToggleUltrathink, onNewSession }) {
  const canSend =
    connection.status === "connected" &&
    chat.composerDraft.trim().length > 0;
  const busy = !!chat.pendingAssistantId;
  const sid = session.activeSid || "(new)";

  return html`
    <section class="xmc-chat" aria-label="chat workspace">
      <header class="xmc-chat__header">
        <div class="xmc-chat__title">
          <strong>会话</strong>
          <code class="xmc-chat__sid">${sid}</code>
        </div>
        <div class="xmc-chat__meta">
          <${Badge} tone=${connection.status === "connected" ? "success" : "warn"}>
            ${connection.status}
          </${Badge}>
          <button
            type="button"
            class="xmc-chat__newbtn"
            onClick=${onNewSession}
            title="新建会话"
          >
            + 新会话
          </button>
        </div>
      </header>
      <${MessageList} messages=${chat.messages} />
      <${Composer}
        value=${chat.composerDraft}
        onChange=${onChangeDraft}
        onSend=${onSend}
        planMode=${chat.planMode}
        onTogglePlan=${onTogglePlan}
        ultrathink=${chat.ultrathink}
        onToggleUltrathink=${onToggleUltrathink}
        canSend=${canSend}
        busy=${busy}
      />
    </section>
  `;
}
