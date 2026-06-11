// XMclaw Agent Artifact Panel — files created/modified by the agent
//
// Shows artefacts (canvas items, created/modified files) with expandable
// previews. Each artefact has type, title, content, file path, and action.

const { h } = window.__xmc.preact;
const { useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

const ACTION_LABELS = {
  create: "created",
  update: "modified",
  delete: "deleted",
};

const TYPE_ICONS = {
  mermaid: "📊",
  html: "🌐",
  svg: "🖼",
  chart: "📈",
  table: "📋",
  file: "📄",
  code: "💻",
  config: "⚙",
};

function ArtifactEntry({ artifact, onOpen }) {
  const [expanded, setExpanded] = useState(false);
  const icon = TYPE_ICONS[artifact.type] || TYPE_ICONS.file;
  const actionLabel = ACTION_LABELS[artifact.action] || artifact.action;

  return html`
    <div class="artifact-entry" key=${artifact.artifactId || artifact.filePath}>
      <span class="artifact-entry__icon">${icon}</span>
      <span class="artifact-entry__title" onClick=${() => setExpanded(!expanded)}>
        ${artifact.title || artifact.filePath || "Untitled"}
      </span>
      <span class="artifact-entry__action">[${actionLabel}]</span>
      ${artifact.filePath ? html`<span class="artifact-entry__path">${artifact.filePath}</span>` : null}
      ${onOpen ? html`
        <button class="artifact-entry__open" onClick=${() => onOpen(artifact)} title="Open in full view">
          ↗
        </button>
      ` : null}
      ${expanded && artifact.content ? html`
        <div class="artifact-entry__preview">
          <pre>${typeof artifact.content === "string" ? artifact.content.slice(0, 2000) : JSON.stringify(artifact.content, null, 2).slice(0, 2000)}</pre>
          ${(artifact.content || "").length > 2000 ? html`<span class="artifact-entry__truncated">…truncated</span>` : null}
        </div>
      ` : null}
    </div>
  `;
}

export function ArtifactPanel({ artifacts = [], onOpenArtifact }) {
  if (!artifacts || artifacts.length === 0) return null;

  return html`
    <div class="artifact-panel" role="region" aria-label="Agent artifacts">
      <div class="artifact-panel__header">
        <span class="artifact-panel__label">📄 Artifacts</span>
        <span class="artifact-panel__count">${artifacts.length} items</span>
      </div>
      <div class="artifact-panel__list">
        ${artifacts.map(a => ArtifactEntry({ artifact: a, onOpen: onOpenArtifact }))}
      </div>
    </div>
  `;
}
