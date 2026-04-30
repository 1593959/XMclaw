// XMclaw — chat-level actions wired into SlashPopover (B-106).
//
// Pulled out of app.js to keep that file under the 500-line UI budget
// enforced by tests/unit/test_v2_ui_scaffold.py. Functions here read /
// mutate the global store + wsHandle that app.js owns — they're
// imported as factory functions so app.js can pass its own handles.

import { toast } from "./toast.js";

/**
 * Build the chat-action helpers bound to a particular store + wsHandle
 * resolver. Returns a plain object of named functions ready to drop
 * into CHAT_ACTIONS.
 *
 * @param {object} ctx
 * @param {object} ctx.store           — XMclaw global Preact store
 * @param {() => object|null} ctx.getWsHandle — returns current WS client
 *   handle (a function so we re-resolve each call — wsHandle can rotate
 *   on session switch, snapshotting once would stale)
 */
export function createChatActions({ store, getWsHandle }) {
  // /retry — copy last user message to composer for review-and-resend.
  function retryLast() {
    const s = store.getState();
    const lastUser = [...(s.chat.messages || [])]
      .reverse()
      .find((m) => m.role === "user" && typeof m.content === "string" && m.content.trim());
    if (!lastUser) {
      toast.info("没有可重发的用户消息");
      return;
    }
    store.setState((cur) => ({
      chat: { ...cur.chat, composerDraft: lastUser.content },
    }));
    toast.info("已填入上一条 prompt — 回车重发，或编辑后再发");
  }

  // /undo — strip last user+assistant turn from UI + ask daemon to do
  // the same on its in-memory session history.
  function undoLast() {
    const ws = getWsHandle();
    if (!ws) {
      toast.error("WS 未连接，undo 失败");
      return;
    }
    const s = store.getState();
    const msgs = s.chat.messages || [];
    let lastUserIdx = -1;
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === "user") { lastUserIdx = i; break; }
    }
    if (lastUserIdx === -1) {
      toast.info("没有可撤销的回合");
      return;
    }
    store.setState((cur) => ({
      chat: {
        ...cur.chat,
        messages: msgs.slice(0, lastUserIdx),
        pendingAssistantId: null,
      },
    }));
    ws.send({ type: "undo" });
    toast.success("已撤销上一回合");
  }

  return { retryLast, undoLast };
}
