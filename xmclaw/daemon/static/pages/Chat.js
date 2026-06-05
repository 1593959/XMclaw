// Worker A — Nebula UI migration (2026-06-05)
// Replaced xmc-* classes with nb-* prefix per Nebula Design System v2.
// HUD, message stream, and bubble structure synced from nebula-prototype.html.
// Data flow (props / store / API) unchanged; pure UI rendering update.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { MessageList } from "../components/molecules/MessageList.js";
import { Composer } from "../components/molecules/Composer.js";
import { ModelPicker } from "../components/molecules/ModelPicker.js";
import { ChatSidebar } from "../components/molecules/ChatSidebar.js";
import { Badge } from "../components/atoms/badge.js";
import { apiGet } from "../lib/api.js";

export function ChatPage({ chat, session, connection, token, onSend, onCancel, onAnswerQuestion, onChangeDraft, onTogglePlan, onCycleOutputStyle, onToggleUltrathink, onNewSession, onResumeSession, onChangeModel, onSwitchAgent, slashStore, onAddImages, onRemoveImage }) {
  const stagedImages = chat.composerImages || [];
  const canSend =
    connection.status === "connected" &&
    (chat.composerDraft.trim().length > 0 || stagedImages.length > 0);
  const busy = !!chat.pendingAssistantId;
  const sid = session.activeSid || "(new)";

  const fmtBytes = (n) => {
    if (n == null || Number.isNaN(n)) return "--";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
    return (n / (1024 * 1024 * 1024)).toFixed(1) + " GB";
  };

  const lastAssistantText = (() => {
    const msgs = chat.messages || [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (m.role !== "assistant") continue;
      if (m.id === chat.pendingAssistantId) continue;
      const text = typeof m.content === "string"
        ? m.content
        : (Array.isArray(m.content)
          ? m.content.map((b) => b?.text || "").join(" ")
          : "");
      return text;
    }
    return "";
  })();

  const isLoading =
    connection.status === "connecting" || connection.status === "reconnecting";

  const [dismissed, setDismissed] = useState(false);
  const [dash, setDash] = useState(null);
  const [skills, setSkills] = useState(null);
  const wsErr = connection.lastError;
  const errKey = `${connection.status}::${wsErr || ""}`;
  useEffect(() => {
    setDismissed(false);
  }, [errKey]);

  useEffect(() => {
    if (!token) return;
    apiGet("/api/v2/dashboard/overview", token)
      .then((data) => setDash(data))
      .catch(() => setDash(null));
  }, [token]);

  useEffect(() => {
    if (!token) return;
    apiGet("/api/v2/skills", token)
      .then((data) => setSkills(Array.isArray(data) ? data : data?.skills || []))
      .catch(() => setSkills(null));
  }, [token]);
  const showError =
    !dismissed &&
    connection.status === "disconnected" &&
    !!wsErr;
  function onRetry() {
    try { window.location.reload(); } catch (_) { /* no-op in tests */ }
  }

  return html`
    <section style="display:flex;height:100vh;overflow:hidden;background:var(--nb-bg-base);" aria-label="chat workspace">
      <div class="nb-chat-layout" style="flex:1;min-width:0;height:100%;">
        <!-- Chat Header -->
        <div class="nb-chat-header">
          <div class="nb-chat-title-area">
            <div class="kick">XMclaw Session</div>
            <h2>${sid}</h2>
          </div>
          <div class="nb-chat-tags">
            <span class="nb-tag accent">${chat.llmProfileId || "default"}</span>
            ${chat.planMode ? html`<span class="nb-tag">plan</span>` : null}
            ${chat.ultrathink ? html`<span class="nb-tag">ultrathink</span>` : null}
            ${(session.agents || []).length > 1 || (session.activeAgentId && session.activeAgentId !== "main")
              ? html`
                  <select
                    class="nb-tag"
                    style="appearance:none;padding-right:20px;background-image:url('data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 width=%2712%27 height=%2712%27 viewBox=%270 0 24 24%27 fill=%27none%27 stroke=%27%2394A3B8%27 stroke-width=%272%27%3E%3Cpath d=%27M6 9l6 6 6-6%27/%3E%3C/svg%3E');background-repeat:no-repeat;background-position:right 6px center;cursor:pointer;"
                    value=${session.activeAgentId || "main"}
                    onChange=${(e) => onSwitchAgent && onSwitchAgent(e.target.value)}
                    title="切换对话目标 agent"
                  >
                    <option value="main">🤖 main (主)</option>
                    ${(session.agents || [])
                      .filter((a) => a.agent_id !== "main")
                      .map((a) => html`<option value=${a.agent_id}>🤝 ${a.agent_id}${a.model ? ` · ${a.model.split("/").pop()}` : ""}</option>`)}
                  </select>
                `
              : null}
            <span class="nb-tag">${connection.status}</span>
            <button
              type="button"
              class="nb-tag accent"
              onClick=${onNewSession}
              title="新建会话"
              style="cursor:pointer;"
            >
              + 新会话
            </button>
          </div>
        </div>

        <!-- HUD 已上移到 AppShell（全 app 通用的 ClawHUD，读 /api/v2/status.telemetry）。
             这里原本手搓的重复栏数据有 bug（记忆显示的是 memory.db 文件大小而非 facts 数、
             候选写死 +1、自主取的是 goal_count），已移除以避免重复+错数。 -->

        <!-- WS Loading Banner -->
        ${isLoading ? html`
          <div
            class="nb-hud"
            role="status"
            aria-live="polite"
            style="margin:0 24px 12px;border-color:rgba(139,92,246,0.3);"
          >
            <div class="nb-hud-seg">
              <span class="nb-status-dot" style="background:var(--nb-accent);box-shadow:0 0 8px var(--nb-accent-glow);"></span>
              <span class="l">连接</span>
              <b>${connection.status === "reconnecting"
                ? `重新连接中… (尝试 ${connection.reconnectAttempt || 1})`
                : "连接中…"}</b>
            </div>
          </div>
        ` : null}

        <!-- WS Error Banner -->
        ${showError ? html`
          <div
            class="nb-hud"
            role="alert"
            style="margin:0 24px 12px;border-color:rgba(239,68,68,0.4);background:linear-gradient(135deg,rgba(239,68,68,0.08),rgba(239,68,68,0.03));"
          >
            <div class="nb-hud-seg" style="flex:1;min-width:0;flex-direction:column;align-items:flex-start;gap:2px;">
              <b style="color:var(--nb-error);">WebSocket 已断开</b>
              <span style="font-size:11px;opacity:.85;word-break:break-word;">${String(wsErr || "未知错误")}</span>
            </div>
            <div class="nb-hud-seg" style="border-right:0;">
              <button type="button" onClick=${onRetry} class="nb-msg-action" style="width:auto;padding:0 10px;font-size:11px;">重试</button>
              <button type="button" onClick=${() => setDismissed(true)} class="nb-msg-action" style="width:auto;padding:0 10px;font-size:11px;" aria-label="关闭">×</button>
            </div>
          </div>
        ` : null}

        <!-- Message Stream -->
        <${MessageList}
          messages=${chat.messages}
          onAnswerQuestion=${onAnswerQuestion}
          pendingAssistantId=${chat.pendingAssistantId}
        />

        <!-- Composer -->
        <div class="nb-composer-area">
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
      </div>

      <${ChatSidebar}
        token=${token}
        activeSid=${session.activeSid}
        connectionStatus=${connection.status}
        toolsCount=${0}
        onNewSession=${onNewSession}
        onResumeSession=${onResumeSession}
      />
    </section>
  `;
}
