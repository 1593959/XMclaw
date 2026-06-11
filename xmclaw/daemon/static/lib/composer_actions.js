// XMclaw — composer-side action helpers
//
// Bundle of the small per-WS-frame senders (send / cancel / answer
// question) plus the local-only state togglers (plan, ultrathink,
// draft). Lives in lib/ so app.js stays under the 500-line UI budget
// (FRONTEND_DESIGN.md §1.4 — hard cap, "超了必须拆"). Same factory
// shape as ``lib/chat_actions.js`` so app.js binds them with one
// ``createComposerActions(...)`` call instead of repeating wsHandle
// access in N free functions.
//
// Why a factory instead of bare exports: the WS handle lives as a
// singleton inside app.js (mutated on every reconnect). Importing it
// directly would freeze the binding at module-load time. The factory
// takes a ``getWsHandle()`` thunk so each call resolves to the
// current WS — same pattern lib/chat_actions.js uses for retryLast /
// undoLast.

export function createComposerActions({
  store,
  getWsHandle,
  toast,
  appendOptimisticUser,
  appendThinkingAssistant,
  appendPromptHistory,
}) {
  function sendComposer() {
    const s = store.getState();
    const text = (s.chat.composerDraft || "").trim();
    const stagedImages = Array.isArray(s.chat.composerImages)
      ? s.chat.composerImages
      : [];
    // Allow sending with images only (no text). The agent can still
    // act on "what's wrong with this screenshot?" with empty text.
    if (!text && stagedImages.length === 0) return;
    const wsHandle = getWsHandle();
    if (!wsHandle) {
      toast.error("WS 未连接，消息未发送 — 请检查 daemon 状态");
      return;
    }
    // B-105: persist this prompt in the up/down history before send.
    try {
      if (text) appendPromptHistory(text);
    } catch (_) { /* never block send on history */ }

    // Allow send even when reconnecting; the WS client now queues frames
    // and flushes them on reconnect (B-13 fix). Without this gate,
    // pressing Enter during a daemon restart would silently lose the
    // message — UI showed an optimistic bubble but the server never
    // got the frame.

    // Optimistic local echo. The daemon will mirror it back as USER_MESSAGE,
    // and the reducer will dedupe by id.
    const { id, chat: afterUser } = appendOptimisticUser(s.chat, text, {
      ultrathink: s.chat.ultrathink,
      images: stagedImages.map((img) => img.dataUrl),
    });
    // Push a "thinking" assistant bubble keyed by `id` so the UI shows
    // immediate feedback. The reducer's llm_chunk / llm_response cases
    // upsert by id, transitioning this bubble into streaming/complete.
    const nextChat = appendThinkingAssistant(afterUser, id);
    store.setState({
      chat: { ...nextChat, composerDraft: "", composerImages: [] },
    });

    // B-MULTIMODAL-UI: include image data URIs in the WS frame so the
    // daemon's WS handler can populate Message.images on the first
    // user turn and the LLM translator encodes them as vision blocks.
    const result = wsHandle.send({
      type: "user",
      content: text,
      images: stagedImages.length > 0
        ? stagedImages.map((img) => img.dataUrl)
        : undefined,
      ultrathink: s.chat.ultrathink || undefined,
      correlation_id: id,
      plan_mode: s.chat.planMode || undefined,
      // Wave-32+: only send when non-default — saves a few bytes
      // and matches the "missing = default" convention the backend
      // expects.
      output_style:
        s.chat.outputStyle && s.chat.outputStyle !== "default"
          ? s.chat.outputStyle
          : undefined,
      llm_profile_id: s.chat.llmProfileId || undefined,
    });

    // Tell the user when the frame is queued vs. sent. Queued frames
    // ride out the reconnect; rejected frames need to be retyped.
    if (result && result.queued) {
      toast.info(
        `当前未连接 daemon，消息已排队 (#${result.pendingCount}) — 重连后自动发送`,
      );
    } else if (result && !result.ok) {
      toast.error("发送失败：" + (result.reason || "未知"));
    }
  }

  // Phase 9 M1: canvas postMessage 桥的回传出口。agent 生成的 html
  // artifact 里调用 window.xmclaw.sendPrompt/submit → CanvasArtifact
  // 捕获 → 这里转成一条普通用户消息发回 agent(走 sendComposer 同款
  // 乐观回显 + thinking 占位 + WS 帧,只是文本来自 widget 而非输入框)。
  function sendCanvasAction({ action, text, data, artifactId, title }) {
    let content = "";
    if (action === "send_prompt") {
      content = (text || "").trim();
    } else if (action === "submit") {
      // 提交带上来源 artifact,agent 能对应回它自己生成的那块 UI。
      content =
        `[界面提交 · ${title || artifactId || "canvas"}]\n` +
        "```json\n" + (data || "{}") + "\n```";
    }
    if (!content) return;
    const wsHandle = getWsHandle();
    if (!wsHandle) {
      toast.error("WS 未连接，操作未发送 — 请检查 daemon 状态");
      return;
    }
    const s = store.getState();
    const { id, chat: afterUser } = appendOptimisticUser(s.chat, content, {});
    const nextChat = appendThinkingAssistant(afterUser, id);
    store.setState({ chat: nextChat });
    const result = wsHandle.send({
      type: "user",
      content,
      correlation_id: id,
    });
    if (result && result.queued) {
      toast.info(`当前未连接 daemon，操作已排队 (#${result.pendingCount}) — 重连后自动发送`);
    } else if (result && !result.ok) {
      toast.error("发送失败：" + (result.reason || "未知"));
    }
  }

  function setLlmProfile(profileId) {
    store.setState((s) => ({
      chat: { ...s.chat, llmProfileId: profileId || null },
    }));
  }

  // B-38: send a cancel frame so the daemon's WS handler signals the
  // running run_turn to bail at its next hop boundary. No-op when no
  // turn is in flight (the server happily processes a stray cancel).
  //
  // B-269: also mark the in-flight turn id as "cancelled" in client
  // state. The reducer's llm_chunk / llm_thinking_chunk cases consult
  // this set and silently drop late-arriving chunks.
  //
  // Wave-32+ UX fix: flip the UI to "stopped" immediately rather than
  // waiting for the backend's cancel_event to reach a hop boundary.
  // The backend may still emit a few more chunks (already in flight
  // when cancel was sent) — those get silently dropped by the B-269
  // gate. From the user's perspective: click Stop → button becomes
  // Send instantly, in-flight assistant bubble shows "已停止" marker
  // and stops ticking, toast confirms in past tense.
  function cancelComposer() {
    const wsHandle = getWsHandle();
    if (!wsHandle) {
      toast.error("WS 未连接");
      return;
    }
    const currentTurnId = store.getState().chat?.pendingAssistantId;
    if (currentTurnId) {
      store.setState((s) => {
        const cancelled = new Set(s.chat.cancelledTurnIds || []);
        cancelled.add(currentTurnId);
        // Mark the in-flight assistant bubble as cancelled — stops
        // its "正在调用 LLM · Ns" spinner and surfaces the terminal
        // state to the user. Skip bubbles already in a terminal
        // state to be safe with race conditions.
        const messages = (s.chat.messages || []).map((m) => {
          if (m.id !== currentTurnId) return m;
          if (m.status === "complete" || m.status === "error" || m.status === "cancelled") {
            return m;
          }
          return { ...m, status: "cancelled", phase: null };
        });
        return {
          ...s,
          chat: {
            ...s.chat,
            cancelledTurnIds: cancelled,
            // Clear pendingAssistantId immediately so `busy` flips
            // false → Stop button becomes Send. Mirrors what the
            // anti_req_violation handler does on terminal events.
            pendingAssistantId: null,
            messages,
          },
        };
      });
    }
    const result = wsHandle.send({ type: "cancel" });
    if (result && !result.ok) {
      toast.error("取消请求失败：" + (result.reason || "未知"));
    } else {
      toast.success("已停止");
    }
  }

  // B-92: forward an answer to the daemon. The QuestionCard built by
  // MessageBubble calls this when the user clicks an option (or types
  // "Other" free text). The daemon's WS handler resolves the in-flight
  // ask_user_question Future and the agent's run_turn loop continues.
  // ``value`` is a string for single-select / Other, or an array for
  // multi-select.
  function answerQuestion(questionId, value) {
    const wsHandle = getWsHandle();
    if (!wsHandle) {
      toast.error("WS 未连接，无法提交回答");
      return;
    }
    const result = wsHandle.send({
      type: "answer_question",
      question_id: questionId,
      value,
    });
    if (result && !result.ok) {
      toast.error("回答提交失败：" + (result.reason || "未知"));
    }
  }

  function changeDraft(value) {
    store.setState((s) => ({ chat: { ...s.chat, composerDraft: value } }));
  }

  function togglePlan() {
    store.setState((s) => ({ chat: { ...s.chat, planMode: !s.chat.planMode } }));
  }

  function toggleUltrathink() {
    store.setState((s) => ({ chat: { ...s.chat, ultrathink: !s.chat.ultrathink } }));
  }

  // Wave-32+ OutputStyles: cycle through the built-in styles. Custom
  // on-disk styles need to be picked via REST; this chip is the
  // quick keyboard-free path for the three defaults.
  function cycleOutputStyle() {
    const order = ["default", "Explanatory", "Learning"];
    store.setState((s) => {
      const cur = s.chat.outputStyle || "default";
      const next = order[(order.indexOf(cur) + 1) % order.length];
      return { chat: { ...s.chat, outputStyle: next } };
    });
  }

  function setOutputStyle(name) {
    store.setState((s) => ({
      chat: { ...s.chat, outputStyle: name || "default" },
    }));
  }

  function addImages(entries) {
    if (!Array.isArray(entries) || entries.length === 0) return;
    store.setState((s) => ({
      chat: {
        ...s.chat,
        composerImages: [
          ...(s.chat.composerImages || []),
          ...entries,
        ],
      },
    }));
  }

  function removeImage(idx) {
    store.setState((s) => {
      const cur = s.chat.composerImages || [];
      const next = cur.filter((_, i) => i !== idx);
      return { chat: { ...s.chat, composerImages: next } };
    });
  }

  // F1 (2026-05-30): user explicitly closed / re-opened the workspace
  // drawer. ``closed=true`` sets ``userClosed`` so further file-change
  // events don't keep popping it open against the user's preference.
  function setWorkspaceClosed(closed) {
    store.setState((s) => {
      const cur = s.chat.workspace || { version: 0, lastChange: null, opened: false };
      return {
        chat: {
          ...s.chat,
          workspace: {
            ...cur,
            opened: !closed,
            userClosed: !!closed,
          },
        },
      };
    });
  }

  return {
    sendComposer,
    sendCanvasAction,
    setLlmProfile,
    cancelComposer,
    answerQuestion,
    changeDraft,
    togglePlan,
    toggleUltrathink,
    cycleOutputStyle,
    setOutputStyle,
    addImages,
    removeImage,
    setWorkspaceClosed,
  };
}
