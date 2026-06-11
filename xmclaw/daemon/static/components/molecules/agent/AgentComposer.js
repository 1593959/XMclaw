// XMclaw Agent Composer — simplified compact input
//
// Single-line textarea with send/stop buttons. Plan/Ultrathink/OutputStyle
// toggles live in the AgentStatusBar as chips. Image attachments are
// supported but collapsed into a pill counter.

const { h } = window.__xmc.preact;
const { useRef, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

export function AgentComposer({
  value = "",
  onChange,
  onSend,
  onCancel,
  busy = false,
  canSend = true,
  images = [],
  onAddImages,
  onRemoveImage,
  onRetry,
}) {
  const inputRef = useRef(null);
  const formRef = useRef(null);

  const handleKeyDown = useCallback((e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (canSend && value.trim()) onSend?.();
    }
    if (e.key === "Escape" && busy) {
      e.preventDefault();
      onCancel?.();
    }
  }, [canSend, value, busy, onSend, onCancel]);

  useEffect(() => {
    if (!busy && inputRef.current) inputRef.current.focus();
  }, [busy]);

  const handlePaste = useCallback((e) => {
    const items = e.clipboardData?.items;
    if (!items || !onAddImages) return;
    for (let i = 0; i < items.length; i++) {
      if (items[i].type.startsWith("image/")) {
        e.preventDefault();
        const file = items[i].getAsFile();
        if (file) onAddImages([file]);
        return;
      }
    }
  }, [onAddImages]);

  return html`
    <div class="agent-composer" ref=${formRef}>
      ${images && images.length > 0 ? html`
        <div class="agent-composer__images">
          ${images.map((img, i) => html`
            <span key=${i} class="agent-composer__image-pill">
              🖼 ${img.name || "image"}
              <button class="agent-composer__image-remove" onClick=${() => onRemoveImage?.(i)}>×</button>
            </span>
          `)}
        </div>
      ` : null}
      <div class="agent-composer__row">
        <textarea
          ref=${inputRef}
          class="agent-composer__input"
          rows="1"
          value=${value}
          onInput=${(e) => onChange?.(e.target.value)}
          onKeyDown=${handleKeyDown}
          onPaste=${handlePaste}
          placeholder=${busy ? "Agent is working…" : "Ask or instruct the agent…"}
          disabled=${busy}
          aria-label="Message input"
        />
        ${busy ? html`
          <button class="agent-composer__btn agent-composer__btn--stop" onClick=${onCancel} title="Stop (Esc)">
            ■ Stop
          </button>
        ` : html`
          ${onRetry ? html`
            <button class="agent-composer__btn agent-composer__btn--retry" onClick=${onRetry} title="Retry last message">
              ↻ Retry
            </button>
          ` : null}
          <button
            class="agent-composer__btn agent-composer__btn--send"
            onClick=${onSend}
            disabled=${!canSend || !value.trim()}
            title="Send (Enter)"
          >
            ↑ Send
          </button>
        `}
      </div>
    </div>
  `;
}
