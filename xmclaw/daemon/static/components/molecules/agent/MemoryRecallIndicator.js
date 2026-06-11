// XMclaw Agent Memory Recall Indicator — shows what memories were recalled
//
// Displays: query that triggered recall, how many hits, and the recalled
// facts with relevance scores and source attribution.
// Located in the right-side Context Panel.

const { h } = window.__xmc.preact;
const { useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

function RecallGroup({ recall }) {
  const [expanded, setExpanded] = useState(false);
  return html`
    <div class="memory-recall-group" key=${recall.id || recall.query}>
      <div class="memory-recall-group__header" onClick=${() => setExpanded(!expanded)}>
        <span class="memory-recall-group__query">"${recall.query || "auto-recall"}"</span>
        <span class="memory-recall-group__count">→ ${recall.hits?.length || 0} hits</span>
        <span class="memory-recall-group__toggle">${expanded ? "▾" : "▸"}</span>
      </div>
      ${expanded && recall.hits ? html`
        <div class="memory-recall-group__hits">
          ${recall.hits.map((hit, i) => html`
            <div key=${i} class="memory-recall-hit">
              <span class="memory-recall-hit__layer">[${hit.layer || hit.kind || "?"}]</span>
              <span class="memory-recall-hit__text">${hit.text || hit.content}</span>
              ${hit.distance != null ? html`<span class="memory-recall-hit__score">(d=${(hit.distance * 100).toFixed(0)}%)</span>` : null}
            </div>
          `)}
        </div>
      ` : null}
    </div>
  `;
}

export function MemoryRecallIndicator({
  memoryOps = [],
  expanded = false,
  onToggle,
}) {
  const recalls = memoryOps.filter(op => op.type === "read");
  if (!recalls || recalls.length === 0) return null;

  return html`
    <div class="memory-recall-indicator" role="region" aria-label="Memory recall">
      <div class="memory-recall-indicator__header" onClick=${onToggle} role="button" tabindex="0" aria-expanded=${expanded}>
        <span class="memory-recall-indicator__label">💭 Memory</span>
        <span class="memory-recall-indicator__count">${recalls.length} recalls</span>
        <span class="memory-recall-indicator__toggle">${expanded ? "▾" : "▸"}</span>
      </div>
      ${expanded ? html`
        <div class="memory-recall-indicator__body">
          ${recalls.map(r => RecallGroup({ recall: r }))}
        </div>
      ` : null}
    </div>
  `;
}
