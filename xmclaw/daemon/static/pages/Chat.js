// XMclaw — Chat page
//
// Composes MessageList + Composer + a small session header. Pure render —
// all WS / store wiring happens in app.js so this file stays trivially
// reusable from a future "session split" view.
//
// Phase B-5: wrapped in Hermes ChatPage's terminal-window chrome (rounded
// frame + dark teal interior + three-dot title bar). Hermes ChatPage.tsx
// embeds an xterm.js TUI inside this frame; we don't have a TUI to
// embed, so the frame hosts our Preact MessageList + Composer instead —
// visual 1:1 at the page-chrome level, content stays XMclaw-native.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

import { MessageList } from "../components/molecules/MessageList.js";
import { Composer } from "../components/molecules/Composer.js";
import { ModelPicker } from "../components/molecules/ModelPicker.js";
import { Badge } from "../components/atoms/badge.js";

export function ChatPage({ chat, session, connection, token, onSend, onChangeDraft, onTogglePlan, onToggleUltrathink, onNewSession, onChangeModel }) {
  const canSend =
    connection.status === "connected" &&
    chat.composerDraft.trim().length > 0;
  const busy = !!chat.pendingAssistantId;
  const sid = session.activeSid || "(new)";

  return html`
    <section class="xmc-h-chat-frame" aria-label="chat workspace">
      <div class="xmc-h-chat-frame__chrome">
        <div class="xmc-h-chat-frame__dots" aria-hidden="true">
          <span class="xmc-h-chat-frame__dot"></span>
          <span class="xmc-h-chat-frame__dot"></span>
          <span class="xmc-h-chat-frame__dot"></span>
        </div>
        <div class="xmc-h-chat-frame__title">
          XMclaw · ${sid}
        </div>
        <button
          type="button"
          class="xmc-h-btn xmc-h-btn--ghost"
          onClick=${onNewSession}
          title="新建会话"
          style="font-size:0.7rem;letter-spacing:0.08em"
        >
          + 新会话
        </button>
      </div>
      <div class="xmc-h-chat-frame__inner xmc-chat">
        <header class="xmc-chat__header">
          <div class="xmc-chat__title">
            <strong>会话</strong>
            <code class="xmc-chat__sid">${sid}</code>
          </div>
          <div class="xmc-chat__meta">
            <${ModelPicker}
              token=${token}
              value=${chat.llmProfileId}
              onChange=${onChangeModel}
            />
            <${Badge} tone=${connection.status === "connected" ? "success" : "warn"}>
              ${connection.status}
            </${Badge}>
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
      </div>
    </section>
  `;
}
