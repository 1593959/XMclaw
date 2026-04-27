// XMclaw — Composer
//
// Textarea + send button + plan/act switch + ultrathink toggle. The
// textarea grows up to 8 lines (≈ 200px) before scrolling, so multi-line
// prompts stay readable.
//
// Keyboard contract (matches FRONTEND_DESIGN.md §7.1):
//   * Enter             → send
//   * Shift+Enter       → newline
//   * Ctrl/Cmd+Enter    → send (mirror of Enter; some keyboards block plain
//                         Enter via IME composition)
//   * Esc               → blur + clear pendingAssistantId hint
//
// We don't disable the input while a turn is streaming — interrupt /
// follow-up framing is part of the agentic UX. The send button itself
// becomes disabled while the socket isn't OPEN to give visible feedback.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

import { Button } from "../atoms/button.js";
import { Badge } from "../atoms/badge.js";
import { usePopoverApi } from "./SlashPopover.js";

export function Composer({
  value,
  onChange,
  onSend,
  planMode,
  onTogglePlan,
  ultrathink,
  onToggleUltrathink,
  canSend,
  busy,
  slashStore,
}) {
  // SlashPopover takeover. When the popover is visible, ↑/↓/Tab/Esc
  // are consumed by it; Enter still falls through to the composer's
  // own send logic so the user can submit "/help" verbatim if they
  // dismiss the popover with Esc first. Mirrors the Hermes Ink TUI.
  const slash = usePopoverApi({
    input: value,
    onApply: (next) => onChange(next),
    store: slashStore || {},
  });

  function handleKeyDown(evt) {
    // Let the SlashPopover claim ↑↓ Tab Esc when it's visible.
    if (slash.handleKey(evt)) return;
    if (evt.key === "Enter" && !evt.shiftKey && !evt.isComposing) {
      evt.preventDefault();
      if (canSend) onSend();
      return;
    }
    if (evt.key === "Enter" && (evt.ctrlKey || evt.metaKey)) {
      evt.preventDefault();
      if (canSend) onSend();
      return;
    }
    if (evt.key === "Escape") {
      evt.target.blur();
    }
  }

  function handleInput(evt) {
    onChange(evt.target.value);
    // Auto-resize: cap at ~200px so the composer doesn't eat the transcript.
    const ta = evt.target;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
  }

  return html`
    <div class="xmc-composer" data-busy=${busy ? "1" : "0"}>
      <div class="xmc-composer__row xmc-composer__row--popover-host">
        ${slash.render()}
        <textarea
          class="xmc-composer__input"
          rows="1"
          placeholder=${planMode
            ? "Plan 模式 — 让助手先规划再执行。Enter 发送，Shift+Enter 换行。"
            : "输入消息… Enter 发送，Shift+Enter 换行。"}
          value=${value}
          onInput=${handleInput}
          onKeyDown=${handleKeyDown}
          aria-label="message composer"
        ></textarea>
        <${Button}
          variant="primary"
          size="md"
          disabled=${!canSend}
          onClick=${() => canSend && onSend()}
          aria-label="send"
        >
          发送
        </${Button}>
      </div>
      <div class="xmc-composer__toolbar">
        <button
          type="button"
          class=${"xmc-composer__chip" + (planMode ? " is-on" : "")}
          aria-pressed=${planMode ? "true" : "false"}
          onClick=${onTogglePlan}
          title="Plan 模式：助手先列计划再执行"
        >
          ${planMode ? "Plan" : "Act"}
        </button>
        <button
          type="button"
          class=${"xmc-composer__chip" + (ultrathink ? " is-on" : "")}
          aria-pressed=${ultrathink ? "true" : "false"}
          onClick=${onToggleUltrathink}
          title="Ultrathink：触发更深的推理（消耗更多 token）"
        >
          ★ Ultrathink
        </button>
        <span class="xmc-composer__hint">
          ${busy
            ? html`<${Badge} tone="info">streaming…</${Badge}>`
            : html`<span class="xmc-composer__shortcut">Enter ⏎</span>`}
        </span>
      </div>
    </div>
  `;
}
