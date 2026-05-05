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

const { h, Component } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

import { MessageBubble } from "./MessageBubble.js";

// B-220: per-bubble error boundary. Pre-B-220 a single bad render
// (e.g. message shape mismatched MessageBubble's expectations) blew
// up the entire React/Preact tree → user saw a fully-black tab.
// Wrapping each row in a boundary isolates the failure: the broken
// bubble shows a small red placeholder; everything else still renders.
class BubbleBoundary extends Component {
  constructor() {
    super();
    this.state = { err: null };
  }
  componentDidCatch(err) {
    // eslint-disable-next-line no-console
    console.error("[xmc] MessageBubble crash:", err);
    this.setState({ err });
  }
  render() {
    if (this.state.err) {
      const msg = String((this.state.err && this.state.err.message) || this.state.err);
      return html`
        <article class="xmc-msg xmc-msg--system" style="color:#c66;font-size:.78rem;font-family:var(--xmc-font-mono);padding:.4rem .8rem;border-left:2px solid #c66">
          [bubble render error] ${msg.slice(0, 200)}
        </article>
      `;
    }
    return this.props.children;
  }
}

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
            (m) => html`
              <${BubbleBoundary} key=${m.id}>
                <${MessageBubble} message=${m} onAnswerQuestion=${onAnswerQuestion} />
              </${BubbleBoundary}>
            `
          )}
    </div>
  `;
}
