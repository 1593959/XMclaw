// XMclaw — AnalyticsPage 1:1 layout port of hermes-agent AnalyticsPage.tsx
//
// Hermes layout (AnalyticsPage.tsx:1-417):
//   1. Top action row: 7d/30d/90d Segmented + Refresh button
//   2. 4 Summary cards (calls / input tokens / output tokens / models used)
//   3. Daily token bar chart (input + output stacked bars)
//   4. Per-model breakdown card (sorted by total tokens, with bar)
//
// Backend: GET /api/v2/analytics?days=N (xmclaw/daemon/routers/analytics.py).

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback, useMemo } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";

function Icon({ d, className }) {
  return html`
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
         class=${"xmc-icon " + (className || "")} aria-hidden="true">
      <path d=${d} />
    </svg>
  `;
}

const I_BAR     = "M3 3v18h18M7 16V10M12 16V6M17 16v-4";
const I_TREND   = "M22 7 13.5 15.5l-5-5L2 17 M16 7h6v6";
const I_HASH    = "M4 9h16 M4 15h16 M10 3 8 21 M16 3l-2 18";
const I_CPU     = "M4 4h16v16H4z M9 9h6v6H9z M9 1v3 M15 1v3 M9 20v3 M15 20v3 M20 9h3 M20 14h3 M1 9h3 M1 14h3";
const I_BRAIN   = "M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3 2.5 2.5 0 0 1 2.46-2.04Z M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3 2.5 2.5 0 0 0-2.46-2.04Z";
const I_REFRESH = "M3 12a9 9 0 0 1 15-6.7L21 8 M21 3v5h-5 M21 12a9 9 0 0 1-15 6.7L3 16 M3 21v-5h5";

const PERIODS = [
  { label: "7d",  days: 7 },
  { label: "30d", days: 30 },
  { label: "90d", days: 90 },
];

function formatTokens(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000)     return (n / 1_000).toFixed(1) + "K";
  return String(n || 0);
}

function formatDate(day) {
  try {
    const d = new Date(day + "T00:00:00");
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  } catch (_) {
    return day;
  }
}

function SummaryCard({ icon, label, value, sub }) {
  return html`
    <div class="xmc-h-card xmc-h-stat">
      <div class="xmc-h-stat__head">
        <span class="xmc-h-stat__label">${label}</span>
        <${Icon} d=${icon} className="xmc-h-stat__icon" />
      </div>
      <div class="xmc-h-stat__value">${value}</div>
      ${sub ? html`<small class="xmc-h-stat__sub">${sub}</small>` : null}
    </div>
  `;
}

// ── Daily token bar chart ────────────────────────────────────────

function TokenBarChart({ daily }) {
  if (!daily || daily.length === 0) {
    return html`<div class="xmc-h-empty">这段时间没有 LLM 调用记录。</div>`;
  }
  const maxT = Math.max(1, ...daily.map((d) => (d.input_tokens || 0) + (d.output_tokens || 0)));
  return html`
    <div class="xmc-h-card xmc-h-chart">
      <header class="xmc-h-chart__head">
        <h3 class="xmc-h-card__title">
          <${Icon} d=${I_BAR} className="xmc-h-icon" />
          每日 token 使用
        </h3>
        <div class="xmc-h-chart__legend">
          <span class="xmc-h-chart__legend-item">
            <span class="xmc-h-chart__swatch xmc-h-chart__swatch--in"></span>
            input
          </span>
          <span class="xmc-h-chart__legend-item">
            <span class="xmc-h-chart__swatch xmc-h-chart__swatch--out"></span>
            output
          </span>
        </div>
      </header>
      <div class="xmc-h-chart__bars" role="img" aria-label="daily token bars">
        ${daily.map((d) => {
          const inH  = ((d.input_tokens  || 0) / maxT) * 100;
          const outH = ((d.output_tokens || 0) / maxT) * 100;
          const total = (d.input_tokens || 0) + (d.output_tokens || 0);
          return html`
            <div
              class="xmc-h-chart__col"
              key=${d.date}
              title=${`${d.date} · ${d.calls} calls · ${formatTokens(total)} tokens`}
            >
              <div class="xmc-h-chart__bar">
                <div class="xmc-h-chart__seg xmc-h-chart__seg--out" style=${"height:" + outH + "%"}></div>
                <div class="xmc-h-chart__seg xmc-h-chart__seg--in"  style=${"height:" + inH + "%"}></div>
              </div>
              <span class="xmc-h-chart__label">${formatDate(d.date)}</span>
            </div>
          `;
        })}
      </div>
    </div>
  `;
}

// ── Per-model breakdown ──────────────────────────────────────────

function ModelTable({ models }) {
  if (!models || models.length === 0) return null;
  const max = Math.max(1, ...models.map((m) => m.input_tokens + m.output_tokens));
  return html`
    <div class="xmc-h-card">
      <h3 class="xmc-h-card__title">
        <${Icon} d=${I_CPU} className="xmc-h-icon" />
        按模型
      </h3>
      <ul class="xmc-h-modeltbl">
        ${models.map((m) => {
          const total = m.input_tokens + m.output_tokens;
          const w = (total / max) * 100;
          return html`
            <li class="xmc-h-modeltbl__row" key=${m.model}>
              <div class="xmc-h-modeltbl__head">
                <code class="xmc-h-modeltbl__name">${m.model}</code>
                <span class="xmc-h-modeltbl__calls">${m.calls} calls</span>
                <span class="xmc-h-modeltbl__tot">${formatTokens(total)} tokens</span>
              </div>
              <div class="xmc-h-modeltbl__track">
                <div class="xmc-h-modeltbl__fill" style=${"width:" + w + "%"}></div>
              </div>
              <div class="xmc-h-modeltbl__split">
                <span><span class="xmc-h-chart__swatch xmc-h-chart__swatch--in"></span>
                in: ${formatTokens(m.input_tokens)}</span>
                <span><span class="xmc-h-chart__swatch xmc-h-chart__swatch--out"></span>
                out: ${formatTokens(m.output_tokens)}</span>
              </div>
            </li>
          `;
        })}
      </ul>
    </div>
  `;
}

// ── Page ────────────────────────────────────────────────────────

export function AnalyticsPage({ token }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [days, setDays] = useState(30);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    apiGet("/api/v2/analytics?days=" + days, token)
      .then((d) => setData(d))
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [days, token]);

  useEffect(() => { load(); }, [load]);

  if (error) {
    return html`
      <section class="xmc-h-page" aria-labelledby="ana-title">
        <header class="xmc-h-page__header">
          <h2 id="ana-title" class="xmc-h-page__title">分析</h2>
        </header>
        <div class="xmc-h-page__body"><div class="xmc-h-error">${error}</div></div>
      </section>
    `;
  }

  return html`
    <section class="xmc-h-page" aria-labelledby="ana-title">
      <header class="xmc-h-page__header">
        <div class="xmc-h-page__heading">
          <h2 id="ana-title" class="xmc-h-page__title">分析</h2>
          <p class="xmc-h-page__subtitle">
            从 events.db 聚合 LLM_RESPONSE 事件 — 按天和按模型展示 token 使用与调用数。
            失败 turn 不计入 token，但仍统计调用次数。
          </p>
        </div>
        <div class="xmc-h-page__actions">
          <div class="xmc-h-segmented" role="group" aria-label="period">
            ${PERIODS.map((p) => html`
              <button
                key=${p.label}
                type="button"
                class=${"xmc-h-segmented__btn " + (days === p.days ? "is-active" : "")}
                onClick=${() => setDays(p.days)}
              >${p.label}</button>
            `)}
          </div>
          <button
            type="button"
            class="xmc-h-btn"
            onClick=${load}
            disabled=${loading}
          >
            <${Icon} d=${I_REFRESH} />
            刷新
          </button>
        </div>
      </header>

      <div class="xmc-h-page__body xmc-h-ana__body">
        ${data === null
          ? html`<div class="xmc-h-loading">载入中…</div>`
          : html`
            <div class="xmc-h-ana__summary">
              <${SummaryCard}
                icon=${I_HASH}
                label="LLM 调用"
                value=${data.summary.total_calls}
                sub=${"近 " + data.period_days + " 天"}
              />
              <${SummaryCard}
                icon=${I_TREND}
                label="输入 token"
                value=${formatTokens(data.summary.total_prompt_tokens)}
                sub="累计"
              />
              <${SummaryCard}
                icon=${I_TREND}
                label="输出 token"
                value=${formatTokens(data.summary.total_completion_tokens)}
                sub="累计"
              />
              <${SummaryCard}
                icon=${I_BRAIN}
                label="使用模型数"
                value=${data.summary.models_used}
                sub=${(data.models[0]?.model || "—").slice(0, 24) + "…"}
              />
            </div>

            <${TokenBarChart} daily=${data.daily} />
            <${ModelTable} models=${data.models} />
          `}
      </div>
    </section>
  `;
}
