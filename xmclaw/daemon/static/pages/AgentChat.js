// XMclaw Agent Chat Page — new agent-style interface
//
// Replaces the old ChatPage with a multi-panel agent layout:
//   AgentStatusBar → PlanPanel → ToolExecutionLog → ThinkingPanel
//   → CompactMessageList → ArtifactPanel → AgentComposer
//
// The sidebar + context panel (Workspace + MemoryRecall) are handled
// by the AppShell layout, not this page.

const { h } = window.__xmc.preact;
const { useState, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { AgentStatusBar } from "../components/molecules/agent/AgentStatusBar.js";
import { PlanPanel } from "../components/molecules/agent/PlanPanel.js";
import { ToolExecutionLog } from "../components/molecules/agent/ToolExecutionLog.js";
import { ThinkingPanel } from "../components/molecules/agent/ThinkingPanel.js";
import { ArtifactPanel } from "../components/molecules/agent/ArtifactPanel.js";
import { CompactMessageList } from "../components/molecules/agent/CompactMessageList.js";
import { AgentComposer } from "../components/molecules/agent/AgentComposer.js";

export function AgentChatPage({
  // Store slices
  chat,
  connection,
  session,
  workspace,
  // Actions
  onSend,
  onCancel,
  onChangeDraft,
  onTogglePlan,
  onAddImages,
  onRemoveImage,
  onToggleUltrathink,
  onCycleOutputStyle,
  onChangeModel,
  onCanvasAction,
  onAnswerQuestion,
  onToggleThinking,
  onToggleMemoryRecall,
  onToggleShowAllTools,
  onConfirmPlan,
  onRejectPlan,
  onOpenArtifact,
  onRetryLast,
  onWorkspaceAction,
}) {
  const busy = chat.pendingAssistantId != null || chat.messages.some(m => m.status === "streaming" || m.status === "thinking");
  const canSend = connection.status === "connected" && !busy;

  return html`
    <div class="agent-chat-page">
      <${AgentStatusBar}
        llmProfileId=${chat.llmProfileId}
        tokenUsage=${chat.tokenUsage}
        sessionElapsed=${chat.sessionElapsed}
        currentHop=${chat.currentHop}
        toolCallCount=${chat.toolCallCount}
        connectionStatus=${connection.status}
      />
      <div class="agent-chat-page__main">
        <div class="agent-chat-page__panels">
          <${PlanPanel}
            planSteps=${chat.planSteps}
            activePlanStep=${chat.activePlanStep}
            planGenerated=${chat.planGenerated}
            planConfirmed=${chat.planConfirmed}
            onConfirmPlan=${onConfirmPlan}
            onRejectPlan=${onRejectPlan}
          />
          <${ToolExecutionLog}
            toolExecutionLog=${chat.toolExecutionLog}
            showAll=${chat.showAllToolCalls}
            maxVisible=${5}
            onToggleShowAll=${onToggleShowAllTools}
          />
          <${ThinkingPanel}
            thinkingSegments=${chat.thinkingSegments}
            collapsed=${chat.thinkingCollapsed}
            onToggle=${onToggleThinking}
          />
          <${ArtifactPanel}
            artifacts=${chat.artifacts}
            onOpenArtifact=${onOpenArtifact}
          />
          <${CompactMessageList}
            messages=${chat.messages}
            maxVisible=${chat.referenceMessageCount}
          />
        </div>
      </div>
      <${AgentComposer}
        value=${chat.composerDraft}
        onChange=${onChangeDraft}
        onSend=${onSend}
        onCancel=${onCancel}
        busy=${busy}
        canSend=${canSend}
        images=${chat.composerImages}
        onAddImages=${onAddImages}
        onRemoveImage=${onRemoveImage}
        onRetry=${onRetryLast}
      />
    </div>
  `;
}
