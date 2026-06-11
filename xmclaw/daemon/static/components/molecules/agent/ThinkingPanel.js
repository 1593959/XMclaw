// XMclaw Agent Thinking Panel — collapsible reasoning display
//
// Shows the agent's chain-of-thought reasoning segments.
// Auto-scrolls when new content streams in.
// Collapsed by default (state stored in chat.thinkingCollapsed).

const { h } = window.__xmc.preact;
const { useState, useRef, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

function fmtChars(n) {
  if (n < 1000) return n + " chars";
  return (n / 1000).toFixed(1) + "k chars";
}

export function ThinkingPanel({
  thinkingSegments = [],
  collapsed = false,
  onToggle,
}) {
  const bottomRef = useRef(null);
  const totalChars = thinkingSegments.reduce((sum, s) => sum + (s.content || "").length, 0);

  useEffect(() => {
    if (bottomRef.current && !collapsed) {
      bottomRef.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [thinkingSegments.length, collapsed]);

  if (!thinkingSegments || thinkingSegments.length === 0) return null;

  return html`
    <div class="thinking-panel" role="region" aria-label="Agent reasoning">
      <div class="thinking-panel__header" onClick=${onToggle} role="button" tabindex="0" aria-expanded=${!collapsed}>
        <span class="thinking-panel__label">💡 Thinking</span>
        <span class="thinking-panel__stats">
          ${thinkingSegments.length} segments, ${fmtChars(totalChars)}
        </span>
        <span class="thinking-panel__toggle">${collapsed ? "[+]" : "[−]"}</span>
      </div>
      ${!collapsed ? html`
        <div class="thinking-panel__body">
          ${thinkingSegments.map((seg) => html`
            <div key=${seg.id} class="thinking-segment">
              <div class="thinking-segment__content">${seg.content}</div>
            </div>
          `)}
          <div ref=${bottomRef} />
        </div>
      ` : null}
    </div>
  `;
}
