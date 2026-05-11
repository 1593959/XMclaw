// XMclaw — Dashboard page (placeholder for Iteration 1)
//
// System overview / landing page. Will be fleshed out in Iteration 6.
// Currently shows a placeholder with the planned surface.

const { h } = window.__xmc.preact;
const html = window.__xmc.htm.bind(h);

export function DashboardPage({ token }) {
  return html`
    <section class="xmc-page" aria-labelledby="dashboard-title">
      <header class="xmc-page__head">
        <h1 id="dashboard-title">概览</h1>
        <p class="xmc-page__subtitle">系统运行状态一览</p>
      </header>

      <div class="xmc-placeholder">
        <h2>即将上线</h2>
        <p class="xmc-placeholder__subtitle">
          系统概览面板正在开发中。
        </p>
        <p class="xmc-placeholder__hint">
          规划功能：daemon 健康状态、活跃会话、最近事件、技能统计、token 消耗。
          <br/>
          见 <code>docs/FRONTEND_REWORK.md §迭代 6</code>。
        </p>
      </div>
    </section>
  `;
}
