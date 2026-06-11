// XMclaw Agent Plan Panel — multi-step plan visualisation
//
// Only visible when the agent is executing a multi-step plan.
// Each step shows a status icon and, when done, duration.
// The user can confirm or reject the plan before execution.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

const STATUS_ICONS = {
  pending:  { icon: "☐", cls: "plan-step--pending" },
  running:  { icon: "⏳", cls: "plan-step--running" },
  done:     { icon: "✅", cls: "plan-step--done" },
  error:    { icon: "❌", cls: "plan-step--error" },
  skipped:  { icon: "⏭", cls: "plan-step--skipped" },
};

function fmtMs(ms) {
  if (!ms) return "";
  if (ms < 1000) return Math.round(ms) + "ms";
  return (ms / 1000).toFixed(1) + "s";
}

export function PlanPanel({
  planSteps = [],
  activePlanStep,
  planGenerated,
  planConfirmed,
  onConfirmPlan,
  onRejectPlan,
  onExecuteStep,
}) {
  if (!planSteps || planSteps.length === 0) {
    if (!planGenerated && !planConfirmed) return null;
    return html`<div class="plan-panel plan-panel--empty">
      <span class="plan-panel__label">📋 Plan</span>
      <span class="plan-panel__hint">Awaiting plan generation…</span>
    </div>`;
  }

  const doneCount = planSteps.filter(s => s.status === "done").length;
  const totalCount = planSteps.length;

  return html`
    <div class="plan-panel" role="region" aria-label="Agent plan">
      <div class="plan-panel__header">
        <span class="plan-panel__label">📋 Plan</span>
        <span class="plan-panel__progress">${doneCount}/${totalCount} steps</span>
        ${!planConfirmed && onConfirmPlan ? html`
          <button class="plan-panel__btn plan-panel__btn--confirm" onClick=${onConfirmPlan}>
            ✓ Confirm
          </button>
          <button class="plan-panel__btn plan-panel__btn--reject" onClick=${onRejectPlan}>
            ✗ Reject
          </button>
        ` : null}
      </div>
      <div class="plan-panel__steps">
        ${planSteps.map((step) => {
          const { icon, cls } = STATUS_ICONS[step.status] || STATUS_ICONS.pending;
          return html`
            <div key=${step.id} class="plan-step ${cls}">
              <span class="plan-step__icon">${icon}</span>
              <span class="plan-step__desc">${step.description}</span>
              ${step.durationMs ? html`<span class="plan-step__time">${fmtMs(step.durationMs)}</span>` : null}
              ${step.status === "running" ? html`<span class="plan-step__pulse" />` : null}
            </div>
          `;
        })}
      </div>
    </div>
  `;
}
