// XMclaw — MessageList
//
// Renders the chat transcript. Uses Preact's keyed reconciliation via the
// stable message.id, so a streaming append into the trailing message only
// re-renders that bubble — no list-wide reflow.
//
// Auto-scrolls the body to the bottom when:
//   * the messages length grows, OR
//   * the trailing message is streaming.
//
// We honor a "pinned" flag stashed on the container element when the user
// scrolls up — once pinned, we stop forcing the scroll. The pin clears when
// they scroll back to the bottom.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

import { MessageBubble } from "./MessageBubble.js";

const SCROLL_PIN_THRESHOLD = 32; // px from bottom

function ensureScrollState(node, lastLen) {
  if (!node) return;
  const distanceFromBottom = node.scrollHeight - node.scrollTop - node.clientHeight;
  // If the user has scrolled up beyond the threshold, set the pin so we
  // don't yank them around mid-read.
  if (distanceFromBottom > SCROLL_PIN_THRESHOLD) {
    node.dataset.xmcPinned = "1";
  } else {
    delete node.dataset.xmcPinned;
  }
  if (node.dataset.xmcPinned !== "1") {
    // Use rAF so we paint after Preact has flushed the DOM.
    requestAnimationFrame(() => {
      node.scrollTop = node.scrollHeight;
    });
  }
  node.dataset.xmcLen = String(lastLen);
}

export function MessageList({ messages, onAnswerQuestion }) {
  const empty = messages.length === 0;
  return html`
    <div
      class="xmc-msglist"
      role="log"
      aria-live="polite"
      aria-relevant="additions text"
      ref=${(node) => ensureScrollState(node, messages.length)}
    >
      ${empty
        ? html`
            <div class="xmc-msglist__empty">
              <p>开始一段对话吧 — 在下方输入消息后回车发送。</p>
              <p class="xmc-msglist__hint">
                Plan 模式下助手会先列出步骤，再等待你确认。Ultrathink 触发更深的推理（消耗更多 token）。
              </p>
            </div>
          `
        : messages.map(
            (m) => html`<${MessageBubble} key=${m.id} message=${m} onAnswerQuestion=${onAnswerQuestion} />`
          )}
    </div>
  `;
}
