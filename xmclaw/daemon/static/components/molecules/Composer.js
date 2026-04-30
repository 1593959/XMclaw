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
//   * ArrowUp on empty  → B-105: pull previous prompt from history
//   * ArrowDown         → B-105: walk back toward most-recent (clears at end)
//
// We don't disable the input while a turn is streaming — interrupt /
// follow-up framing is part of the agentic UX. The send button itself
// becomes disabled while the socket isn't OPEN to give visible feedback.

const { h } = window.__xmc.preact;
const { useState, useRef, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Button } from "../atoms/button.js";
import { Badge } from "../atoms/badge.js";
import { usePopoverApi } from "./SlashPopover.js";
import { createRecognizer, sttSupported } from "../../lib/audio.js";
import { toast } from "../../lib/toast.js";

// B-105: prompt history picker (free-code HISTORY_PICKER parity).
// Stores the last 50 distinct user-sent prompts in localStorage so
// Up/Down on an empty composer cycle through past prompts. Same UX
// as a shell history. Keyed by ``xmc-prompt-history-v1``; old entries
// roll off as the cap fills.
const _HISTORY_KEY = "xmc-prompt-history-v1";
const _HISTORY_MAX = 50;

function _readHistory() {
  try {
    const raw = localStorage.getItem(_HISTORY_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.filter((s) => typeof s === "string") : [];
  } catch (_) {
    return [];
  }
}

function _writeHistory(list) {
  try {
    localStorage.setItem(_HISTORY_KEY, JSON.stringify(list.slice(-_HISTORY_MAX)));
  } catch (_) {
    /* private mode / quota — fail silent */
  }
}

export function appendPromptHistory(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  const cur = _readHistory();
  // Drop adjacent duplicates so spamming Enter on the same prompt
  // doesn't fill the buffer.
  if (cur.length > 0 && cur[cur.length - 1] === trimmed) return;
  cur.push(trimmed);
  _writeHistory(cur);
}

export function Composer({
  value,
  onChange,
  onSend,
  onCancel,
  planMode,
  onTogglePlan,
  ultrathink,
  onToggleUltrathink,
  canSend,
  busy,
  slashStore,
  token,
}) {
  // SlashPopover takeover. When the popover is visible, ↑/↓/Tab/Esc
  // are consumed by it; Enter still falls through to the composer's
  // own send logic so the user can submit "/help" verbatim if they
  // dismiss the popover with Esc first. Mirrors the Hermes Ink TUI.
  const slash = usePopoverApi({
    input: value,
    onApply: (next) => onChange(next),
    store: slashStore || {},
    token,
  });

  // ── Mic / STT (B-20) ─────────────────────────────────────────────
  // Click the mic to dictate. Live partial transcript replaces a
  // "[听写中…]" placeholder; final result becomes the actual draft.
  // The recognizer is one-shot — auto-stops after the user pauses.
  const [listening, setListening] = useState(false);
  const recRef = useRef(null);
  const baseTextRef = useRef("");

  // B-105: prompt history pointer. Refreshed-from-localStorage on every
  // ↑ press (single source of truth = localStorage so multi-tab stays
  // consistent without subscribers). ``idx === null`` means "not
  // browsing history; user is typing fresh".
  const historyIdxRef = useRef(null);
  const draftBeforeHistoryRef = useRef("");

  useEffect(() => () => {
    if (recRef.current) recRef.current.stop();
  }, []);

  const startListening = () => {
    if (!sttSupported) {
      toast.error("当前浏览器不支持语音输入（建议 Chrome 或 Edge）");
      return;
    }
    if (recRef.current?.isActive?.()) {
      recRef.current.stop();
      return;
    }
    baseTextRef.current = value || "";
    const rec = createRecognizer({
      onPartial: (interim) => {
        const sep = baseTextRef.current && !baseTextRef.current.endsWith(" ") ? " " : "";
        onChange(baseTextRef.current + sep + interim);
      },
      onFinal: (final) => {
        const sep = baseTextRef.current && !baseTextRef.current.endsWith(" ") ? " " : "";
        baseTextRef.current = baseTextRef.current + sep + final;
        onChange(baseTextRef.current);
      },
      onError: (err) => {
        setListening(false);
        const msg = err?.message || String(err) || "语音识别失败";
        if (msg !== "no-speech" && msg !== "aborted") {
          toast.error("语音识别：" + msg);
        }
      },
      onEnd: () => setListening(false),
    });
    recRef.current = rec;
    rec.start();
    setListening(true);
  };

  function handleKeyDown(evt) {
    // Let the SlashPopover claim ↑↓ Tab Esc when it's visible.
    if (slash.handleKey(evt)) return;
    if (evt.key === "Enter" && !evt.shiftKey && !evt.isComposing) {
      evt.preventDefault();
      if (canSend) {
        historyIdxRef.current = null;  // reset history cursor on send
        onSend();
      }
      return;
    }
    if (evt.key === "Enter" && (evt.ctrlKey || evt.metaKey)) {
      evt.preventDefault();
      if (canSend) {
        historyIdxRef.current = null;
        onSend();
      }
      return;
    }
    if (evt.key === "Escape") {
      historyIdxRef.current = null;
      evt.target.blur();
      return;
    }
    // B-105: prompt history. ArrowUp pulls older prompts; ArrowDown
    // walks toward newer ones. Only triggers when the cursor is on
    // a single-line view (no internal newlines so ↑↓ aren't navigating
    // the textarea content) — keeps multi-line editing unbroken.
    if (evt.key === "ArrowUp" && !evt.shiftKey && !evt.altKey) {
      const ta = evt.target;
      const v = ta.value || "";
      // Only intercept when textarea cursor is on the FIRST line — same
      // heuristic free-code uses. Multi-line drafts behave normally.
      const caretAtStart = ta.selectionStart === 0 || !v.slice(0, ta.selectionStart).includes("\n");
      if (!caretAtStart) return;
      const hist = _readHistory();
      if (hist.length === 0) return;
      // Save the live draft on the first up so down can restore it.
      if (historyIdxRef.current === null) {
        draftBeforeHistoryRef.current = v;
        historyIdxRef.current = hist.length - 1;
      } else if (historyIdxRef.current > 0) {
        historyIdxRef.current -= 1;
      } else {
        return;  // already at oldest
      }
      evt.preventDefault();
      onChange(hist[historyIdxRef.current]);
      return;
    }
    if (evt.key === "ArrowDown" && !evt.shiftKey && !evt.altKey) {
      if (historyIdxRef.current === null) return;
      const ta = evt.target;
      const v = ta.value || "";
      const caretAtEnd =
        ta.selectionStart === v.length || !v.slice(ta.selectionStart).includes("\n");
      if (!caretAtEnd) return;
      const hist = _readHistory();
      if (historyIdxRef.current >= hist.length - 1) {
        // Walked back past most-recent — restore live draft.
        historyIdxRef.current = null;
        evt.preventDefault();
        onChange(draftBeforeHistoryRef.current);
        return;
      }
      historyIdxRef.current += 1;
      evt.preventDefault();
      onChange(hist[historyIdxRef.current]);
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
        <button
          type="button"
          class=${"xmc-composer__mic" + (listening ? " is-on" : "") + (sttSupported ? "" : " is-disabled")}
          onClick=${startListening}
          disabled=${!sttSupported}
          aria-pressed=${listening ? "true" : "false"}
          aria-label=${listening ? "停止听写" : "开始语音输入"}
          title=${sttSupported
            ? (listening ? "停止听写（再次点击）" : "语音输入 — 点击开始说话")
            : "当前浏览器不支持语音输入"}
        >
          ${listening ? "🔴" : "🎙"}
        </button>
        ${busy && onCancel
          ? html`<${Button}
              variant="danger"
              size="md"
              onClick=${() => onCancel()}
              aria-label="stop"
              title="停止当前回答（在 hop 边界生效）"
            >
              ⏹ 停止
            </${Button}>`
          : html`<${Button}
              variant="primary"
              size="md"
              disabled=${!canSend}
              onClick=${() => canSend && onSend()}
              aria-label="send"
            >
              发送
            </${Button}>`}
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
