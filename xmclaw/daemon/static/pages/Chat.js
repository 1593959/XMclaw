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
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { MessageList } from "../components/molecules/MessageList.js";
import { Composer } from "../components/molecules/Composer.js";
import { ModelPicker } from "../components/molecules/ModelPicker.js";
import { ChatSidebar } from "../components/molecules/ChatSidebar.js";
import { Badge } from "../components/atoms/badge.js";

// Audit pass-3 B1+B4: Chat now renders an explicit loading state while the
// WS handshakes and a dismissable error banner when the connection drops
// or fails. Reads `connection.status / lastError / reconnectAttempt`
// already tracked by app.js's WS reducer — does NOT add new state to the
// store. The retry button just calls `window.location.reload()` because
// the WS client owns its own backoff loop and exposing a manual reconnect
// hook through the page tree is out of scope for this batch.

export function ChatPage({ chat, session, connection, token, onSend, onCancel, onAnswerQuestion, onChangeDraft, onTogglePlan, onCycleOutputStyle, onToggleUltrathink, onNewSession, onResumeSession, onChangeModel, onSwitchAgent, slashStore, onAddImages, onRemoveImage }) {
  const stagedImages = chat.composerImages || [];
  const canSend =
    connection.status === "connected" &&
    (chat.composerDraft.trim().length > 0 || stagedImages.length > 0);
  const busy = !!chat.pendingAssistantId;
  const sid = session.activeSid || "(new)";

  // Wave 7: feed the latest finalized assistant message to the
  // Composer so its continuous-voice loop can TTS-read it when the
  // turn ends.
  const lastAssistantText = (() => {
    const msgs = chat.messages || [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (m.role !== "assistant") continue;
      if (m.id === chat.pendingAssistantId) continue;  // still streaming
      const text = typeof m.content === "string"
        ? m.content
        : (Array.isArray(m.content)
          ? m.content.map((b) => b?.text || "").join(" ")
          : "");
      return text;
    }
    return "";
  })();

  // B1: visible-at-top loading dot during connect/reconnect — kept inline
  // so the message list stays mounted and the user doesn't lose scroll
  // position on a transient drop.
  const isLoading =
    connection.status === "connecting" || connection.status === "reconnecting";

  // B4: error banner for full disconnect (status === "disconnected" with
  // a real lastError). `dismissed` is local — once hidden the banner
  // stays hidden until a NEW disconnect happens (different errKey).
  const [dismissed, setDismissed] = useState(false);
  const wsErr = connection.lastError;
  const errKey = `${connection.status}::${wsErr || ""}`;
  // Reset dismissal whenever the error identity changes so a fresh
  // disconnect re-shows the banner. useEffect (post-render) avoids the
  // setState-during-render footgun.
  useEffect(() => {
    setDismissed(false);
  }, [errKey]);
  const showError =
    !dismissed &&
    connection.status === "disconnected" &&
    !!wsErr;
  function onRetry() {
    // The WS client backs off exponentially on its own; a hard reload is
    // the simplest user-initiated reconnect that doesn't require plumbing
    // a new prop through app.js. Acceptable for an error-state retry —
    // not a steady-state hot path.
    try { window.location.reload(); } catch (_) { /* no-op in tests */ }
  }

  return html`
    <section class="xmc-h-chat-frame" aria-label="chat workspace">
      <div class="xmc-h-chat-frame__body">
       <div class="xmc-h-chat-frame__inner xmc-chat">
        <header class="xmc-chat__header">
          <div class="xmc-chat__title">
            <strong>XMclaw</strong>
            <code class="xmc-chat__sid">${sid}</code>
          </div>
          <div class="xmc-chat__meta">
            ${(session.agents || []).length > 1 || (session.activeAgentId && session.activeAgentId !== "main")
              ? html`
                  <select
                    class="xmc-h-btn xmc-h-btn--ghost"
                    style="font-size:.72rem;padding:.18rem .35rem"
                    value=${session.activeAgentId || "main"}
                    onChange=${(e) => onSwitchAgent && onSwitchAgent(e.target.value)}
                    title="切换对话目标 agent (B-133)"
                  >
                    <option value="main">🤖 main (主)</option>
                    ${(session.agents || [])
                      .filter((a) => a.agent_id !== "main")
                      .map((a) => html`<option value=${a.agent_id}>🤝 ${a.agent_id}${a.model ? ` · ${a.model.split("/").pop()}` : ""}</option>`)}
                  </select>
                `
              : null}
            <${ModelPicker}
              token=${token}
              value=${chat.llmProfileId}
              onChange=${onChangeModel}
            />
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
        ${isLoading ? html`
          <div
            class="xmc-h-loading"
            role="status"
            aria-live="polite"
            style="padding:.5rem .75rem;font-size:.72rem"
          >
            ${connection.status === "reconnecting"
              ? `重新连接中… (尝试 ${connection.reconnectAttempt || 1})`
              : "连接中…"}
          </div>
        ` : null}
        ${showError ? html`
          <div
            class="xmc-h-error"
            role="alert"
            style="display:flex;gap:.6rem;align-items:center;justify-content:space-between;margin:.4rem .25rem"
          >
            <div style="flex:1;min-width:0">
              <strong>WebSocket 已断开</strong>
              <div style="font-size:.78rem;opacity:.85;margin-top:2px;word-break:break-word">
                ${String(wsErr || "未知错误")}
              </div>
            </div>
            <div style="display:flex;gap:.4rem;flex-shrink:0">
              <button type="button" onClick=${onRetry} class="xmc-h-btn">重试</button>
              <button type="button" onClick=${() => setDismissed(true)} class="xmc-h-btn xmc-h-btn--ghost" aria-label="关闭">×</button>
            </div>
          </div>
        ` : null}
        <${MessageList} messages=${chat.messages} onAnswerQuestion=${onAnswerQuestion} />
        <${Composer}
          value=${chat.composerDraft}
          onChange=${onChangeDraft}
          onSend=${onSend}
          onCancel=${onCancel}
          planMode=${chat.planMode}
          onTogglePlan=${onTogglePlan}
          outputStyle=${chat.outputStyle}
          onCycleOutputStyle=${onCycleOutputStyle}
          ultrathink=${chat.ultrathink}
          onToggleUltrathink=${onToggleUltrathink}
          canSend=${canSend}
          busy=${busy}
          slashStore=${slashStore}
          token=${token}
          images=${stagedImages}
          onAddImages=${onAddImages}
          onRemoveImage=${onRemoveImage}
          lastAssistantText=${lastAssistantText}
        />
       </div>
       <${ChatSidebar}
         token=${token}
         activeSid=${session.activeSid}
         connectionStatus=${connection.status}
         toolsCount=${0}
         onNewSession=${onNewSession}
         onResumeSession=${onResumeSession}
       />
      </div>
    </section>
  `;
}
