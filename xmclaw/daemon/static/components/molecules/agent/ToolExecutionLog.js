// XMclaw Agent Tool Execution Log — real-time tool call feed
//
// Extracted from message bubbles into a dedicated scrollable list.
// Shows max 5 recent calls by default with "Show all" expander.
// Each entry: tool name, args preview, status icon, duration.

const { h } = window.__xmc.preact;
const { useState, useRef, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

const STATUS_META = {
  pending: { icon: "○", label: "pending", cls: "tool-entry--pending" },
  running: { icon: "◉", label: "running", cls: "tool-entry--running" },
  done:    { icon: "✓", label: "done",    cls: "tool-entry--done" },
  error:   { icon: "✗", label: "error",   cls: "tool-entry--error" },
};

function fmtMs(ms) {
  if (!ms && ms !== 0) return "";
  if (ms < 1000) return Math.round(ms) + "ms";
  return (ms / 1000).toFixed(1) + "s";
}

function truncArgs(args, maxLen = 60) {
  const s = typeof args === "string" ? args : JSON.stringify(args || {});
  if (s.length <= maxLen) return s;
  return s.slice(0, maxLen) + "…";
}

function ToolEntry({ entry, isExpanded, onToggle }) {
  const meta = STATUS_META[entry.status] || STATUS_META.pending;
  return html`
    <div class="tool-entry ${meta.cls}" key=${entry.id}>
      <span class="tool-entry__icon" title=${meta.label}>${meta.icon}</span>
      <span class="tool-entry__name" onClick=${onToggle}>${entry.toolName}</span>
      <span class="tool-entry__args">${truncArgs(entry.args)}</span>
      <span class="tool-entry__time">${fmtMs(entry.durationMs)}</span>
      ${entry.status === "running" ? html`<span class="tool-entry__spinner" />` : null}
      ${isExpanded && entry.result ? html`
        <div class="tool-entry__result">
          <pre>${typeof entry.result === "string" ? entry.result : JSON.stringify(entry.result, null, 2)}</pre>
        </div>
      ` : null}
      ${isExpanded && entry.error ? html`
        <div class="tool-entry__error">
          <pre>${entry.error}</pre>
        </div>
      ` : null}
    </div>
  `;
}

export function ToolExecutionLog({
  toolExecutionLog = [],
  showAll = false,
  maxVisible = 5,
  onToggleShowAll,
}) {
  const [expandedIds, setExpandedIds] = useState(new Set());
  const bottomRef = useRef(null);

  const runningCount = toolExecutionLog.filter(e => e.status === "running").length;
  const doneCount = toolExecutionLog.filter(e => e.status === "done").length;
  const errorCount = toolExecutionLog.filter(e => e.status === "error").length;

  const visible = showAll ? toolExecutionLog : toolExecutionLog.slice(-maxVisible);
  const hiddenCount = toolExecutionLog.length - visible.length;

  useEffect(() => {
    if (bottomRef.current) bottomRef.current.scrollIntoView({ behavior: "smooth" });
  }, [toolExecutionLog.length]);

  if (!toolExecutionLog || toolExecutionLog.length === 0) return null;

  return html`
    <div class="tool-log" role="region" aria-label="Tool execution log">
      <div class="tool-log__header">
        <span class="tool-log__label">🔧 Tools</span>
        <span class="tool-log__stats">
          ${runningCount > 0 ? html`<span class="tool-log__stat--running">${runningCount} running</span>` : null}
          ${doneCount > 0 ? html`<span class="tool-log__stat--done">${doneCount} done</span>` : null}
          ${errorCount > 0 ? html`<span class="tool-log__stat--error">${errorCount} failed</span>` : null}
        </span>
      </div>
      <div class="tool-log__entries">
        ${visible.map(entry => ToolEntry({
          entry,
          isExpanded: expandedIds.has(entry.id),
          onToggle: () => {
            const next = new Set(expandedIds);
            if (next.has(entry.id)) next.delete(entry.id); else next.add(entry.id);
            setExpandedIds(next);
          },
        }))}
        <div ref=${bottomRef} />
      </div>
      ${hiddenCount > 0 ? html`
        <button class="tool-log__show-all" onClick=${onToggleShowAll}>
          ${showAll ? "Show fewer ↑" : `Show all ${toolExecutionLog.length} calls ↓`}
        </button>
      ` : null}
    </div>
  `;
}
