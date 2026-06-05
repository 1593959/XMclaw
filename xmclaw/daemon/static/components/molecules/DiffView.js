// XMclaw — Diff View (molecule)
//
// Side-by-side diff: left = removed (red), right = added (green).
// Each line is tagged removed / added / unchanged. Headers show
// the left / right file titles.
//
// Props:
//   leftTitle   string         – header for the left column
//   rightTitle  string         – header for the right column
//   lines       Array<{type, text}>  – type: "removed" | "added" | "unchanged"

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

export function DiffView({ leftTitle = "Before", rightTitle = "After", lines = [] }) {
  const leftLines = lines.filter((l) => l.type !== "added");
  const rightLines = lines.filter((l) => l.type !== "removed");

  return html`
    <div class="nb-diff" role="region" aria-label="代码对比">
      <div class="nb-diff__side left">
        <div class="nb-diff__header left">${leftTitle}</div>
        ${leftLines.map((l, i) => html`
          <div key=${"l" + i} class="nb-diff__line ${l.type}">${l.text}</div>
        `)}
      </div>
      <div class="nb-diff__side right">
        <div class="nb-diff__header right">${rightTitle}</div>
        ${rightLines.map((l, i) => html`
          <div key=${"r" + i} class="nb-diff__line ${l.type}">${l.text}</div>
        `)}
      </div>
    </div>
  `;
}
