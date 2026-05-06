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
const I_TOOL    = "M14.7 6.3a4 4 0 0 1 0 5.66l-1.4 1.42-5.66-5.66 1.42-1.41a4 4 0 0 1 5.65 0Z M3 21l5.66-5.66 M9.36 13l-5.65 5.66 M21 3l-7 7";
const I_GLOBE   = "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Z M2 12h20 M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10Z";
const I_CLOCK   = "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Z M12 6v6l4 2";
const I_LIST    = "M8 6h13 M8 12h13 M8 18h13 M3 6h.01 M3 12h.01 M3 18h.01";
const I_DOLLAR  = "M12 1v22 M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6";
const I_ALERT   = "M12 9v4 M12 17h.01 M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z";

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

// ── Tools / Platforms / Activity / Top sessions (B-228) ─────────

const PLATFORM_LABELS = {
  web:     "Web UI (chat-)",
  feishu:  "飞书",
  reflect: "Reflect 自反思",
  dream:   "Dream 离线训练",
  probe:   "Probe / E2E",
  other:   "其他",
};

const WEEKDAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"];

function formatRelativeTime(ts) {
  if (!ts) return "—";
  const diff = Math.floor(Date.now() / 1000 - ts);
  if (diff < 60) return "刚刚";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  return Math.floor(diff / 86400) + "d ago";
}

function ToolsTable({ tools }) {
  if (!tools || tools.length === 0) return null;
  const max = Math.max(1, ...tools.map((t) => t.calls));
  return html`
    <div class="xmc-h-card">
      <h3 class="xmc-h-card__title">
        <${Icon} d=${I_TOOL} className="xmc-h-icon" />
        工具调用 (Top ${tools.length})
      </h3>
      <ul class="xmc-h-modeltbl">
        ${tools.map((t) => {
          const w = (t.calls / max) * 100;
          const pct = ((t.error_rate || 0) * 100).toFixed(1);
          return html`
            <li class="xmc-h-modeltbl__row" key=${t.name}>
              <div class="xmc-h-modeltbl__head">
                <code class="xmc-h-modeltbl__name">${t.name}</code>
                <span class="xmc-h-modeltbl__calls">${t.calls} calls</span>
                <span class="xmc-h-modeltbl__tot">
                  ${t.errors > 0 ? `${t.errors} err · ${pct}%` : "0 err"}
                </span>
              </div>
              <div class="xmc-h-modeltbl__track">
                <div class="xmc-h-modeltbl__fill" style=${"width:" + w + "%"}></div>
              </div>
            </li>
          `;
        })}
      </ul>
    </div>
  `;
}

function PlatformsTable({ platforms }) {
  if (!platforms || platforms.length === 0) return null;
  const max = Math.max(1, ...platforms.map((p) => p.calls));
  return html`
    <div class="xmc-h-card">
      <h3 class="xmc-h-card__title">
        <${Icon} d=${I_GLOBE} className="xmc-h-icon" />
        来源渠道
      </h3>
      <ul class="xmc-h-modeltbl">
        ${platforms.map((p) => {
          const total = (p.input_tokens || 0) + (p.output_tokens || 0);
          const w = (p.calls / max) * 100;
          const label = PLATFORM_LABELS[p.platform] || p.platform;
          return html`
            <li class="xmc-h-modeltbl__row" key=${p.platform}>
              <div class="xmc-h-modeltbl__head">
                <code class="xmc-h-modeltbl__name">${label}</code>
                <span class="xmc-h-modeltbl__calls">${p.calls} calls</span>
                <span class="xmc-h-modeltbl__tot">${formatTokens(total)} tokens</span>
              </div>
              <div class="xmc-h-modeltbl__track">
                <div class="xmc-h-modeltbl__fill" style=${"width:" + w + "%"}></div>
              </div>
              <div class="xmc-h-modeltbl__split">
                <span><span class="xmc-h-chart__swatch xmc-h-chart__swatch--in"></span>
                in: ${formatTokens(p.input_tokens || 0)}</span>
                <span><span class="xmc-h-chart__swatch xmc-h-chart__swatch--out"></span>
                out: ${formatTokens(p.output_tokens || 0)}</span>
              </div>
            </li>
          `;
        })}
      </ul>
    </div>
  `;
}

function ActivityChart({ activity }) {
  if (!activity) return null;
  const wd = activity.by_weekday || [];
  const hr = activity.by_hour || [];
  if (wd.length === 0 && hr.length === 0) return null;
  const wdMax = Math.max(1, ...wd);
  const hrMax = Math.max(1, ...hr);
  return html`
    <div class="xmc-h-card">
      <h3 class="xmc-h-card__title">
        <${Icon} d=${I_CLOCK} className="xmc-h-icon" />
        活跃分布
      </h3>
      <div style="font-size:12px;opacity:.6;margin:4px 0 6px">按星期 (UTC)</div>
      <div class="xmc-h-chart__bars" style="height:80px;display:flex;align-items:flex-end;gap:6px;margin-bottom:16px">
        ${wd.map((c, i) => {
          const hh = (c / wdMax) * 100;
          return html`
            <div class="xmc-h-chart__col" key=${"wd-" + i} title=${`周${WEEKDAY_LABELS[i]} · ${c} calls`}>
              <div class="xmc-h-chart__bar">
                <div class="xmc-h-chart__seg xmc-h-chart__seg--in" style=${"height:" + hh + "%"}></div>
              </div>
              <span class="xmc-h-chart__label">${WEEKDAY_LABELS[i]}</span>
            </div>
          `;
        })}
      </div>
      <div style="font-size:12px;opacity:.6;margin:4px 0 6px">按小时 (UTC, 0-23)</div>
      <div class="xmc-h-chart__bars" style="height:80px;display:flex;align-items:flex-end;gap:3px">
        ${hr.map((c, i) => {
          const hh = (c / hrMax) * 100;
          return html`
            <div class="xmc-h-chart__col" key=${"hr-" + i} title=${`${i}:00 · ${c} calls`}>
              <div class="xmc-h-chart__bar">
                <div class="xmc-h-chart__seg xmc-h-chart__seg--in" style=${"height:" + hh + "%"}></div>
              </div>
              <span class="xmc-h-chart__label">${i % 6 === 0 ? String(i) : ""}</span>
            </div>
          `;
        })}
      </div>
    </div>
  `;
}

function formatCost(usd) {
  if (!usd && usd !== 0) return "—";
  if (usd < 0.01) return "$" + usd.toFixed(4);
  if (usd < 1) return "$" + usd.toFixed(3);
  if (usd < 100) return "$" + usd.toFixed(2);
  return "$" + Math.round(usd).toLocaleString();
}

function TopErrorsTable({ errors }) {
  if (!errors || errors.length === 0) return null;
  const max = Math.max(1, ...errors.map((e) => e.count));
  return html`
    <div class="xmc-h-card">
      <h3 class="xmc-h-card__title">
        <${Icon} d=${I_ALERT} className="xmc-h-icon" />
        Top errors (按频率)
      </h3>
      <ul class="xmc-h-modeltbl">
        ${errors.map((e) => {
          const w = (e.count / max) * 100;
          const errStr = e.error || "unknown";
          const display = errStr.length > 80 ? errStr.slice(0, 80) + "…" : errStr;
          return html`
            <li class="xmc-h-modeltbl__row" key=${errStr}>
              <div class="xmc-h-modeltbl__head">
                <code class="xmc-h-modeltbl__name" title=${errStr}>${display}</code>
                <span class="xmc-h-modeltbl__tot">${e.count} 次</span>
              </div>
              <div class="xmc-h-modeltbl__track">
                <div class="xmc-h-modeltbl__fill" style=${"width:" + w + "%"}></div>
              </div>
            </li>
          `;
        })}
      </ul>
    </div>
  `;
}

function TopSessionsTable({ sessions }) {
  if (!sessions || sessions.length === 0) return null;
  const max = Math.max(1, ...sessions.map((s) => s.total_tokens || 0));
  return html`
    <div class="xmc-h-card">
      <h3 class="xmc-h-card__title">
        <${Icon} d=${I_LIST} className="xmc-h-icon" />
        Top sessions (按 token 消耗)
      </h3>
      <ul class="xmc-h-modeltbl">
        ${sessions.map((s) => {
          const total = s.total_tokens || 0;
          const w = (total / max) * 100;
          const sid = s.session_id || "";
          const display = sid.length > 40 ? sid.slice(0, 40) + "…" : sid;
          const platLabel = PLATFORM_LABELS[s.platform] || s.platform;
          return html`
            <li class="xmc-h-modeltbl__row" key=${sid}>
              <div class="xmc-h-modeltbl__head">
                <code class="xmc-h-modeltbl__name" title=${sid}>${display}</code>
                <span class="xmc-h-modeltbl__calls">${platLabel} · ${s.calls} calls</span>
                <span class="xmc-h-modeltbl__tot">${formatTokens(total)} · ${formatRelativeTime(s.last_ts)}</span>
              </div>
              <div class="xmc-h-modeltbl__track">
                <div class="xmc-h-modeltbl__fill" style=${"width:" + w + "%"}></div>
              </div>
              <div class="xmc-h-modeltbl__split">
                <span><span class="xmc-h-chart__swatch xmc-h-chart__swatch--in"></span>
                in: ${formatTokens(s.input_tokens || 0)}</span>
                <span><span class="xmc-h-chart__swatch xmc-h-chart__swatch--out"></span>
                out: ${formatTokens(s.output_tokens || 0)}</span>
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
              <${SummaryCard}
                icon=${I_DOLLAR}
                label="估算成本"
                value=${formatCost(data.summary.total_cost_usd || 0)}
                sub="按公开价目, heuristic"
              />
              ${(data.summary.total_failed_calls || 0) > 0 ? html`
                <${SummaryCard}
                  icon=${I_ALERT}
                  label="失败调用"
                  value=${data.summary.total_failed_calls}
                  sub="LLM 报错次数"
                />
              ` : null}
            </div>

            <${TokenBarChart} daily=${data.daily} />
            <${ModelTable} models=${data.models} />
            <!-- B-228: extended dimensions (tools / platforms / activity / top_sessions) -->
            ${(data.tools || []).length > 0 ? html`<${ToolsTable} tools=${data.tools} />` : null}
            ${(data.platforms || []).length > 0 ? html`<${PlatformsTable} platforms=${data.platforms} />` : null}
            ${data.activity ? html`<${ActivityChart} activity=${data.activity} />` : null}
            ${(data.top_sessions || []).length > 0 ? html`<${TopSessionsTable} sessions=${data.top_sessions} />` : null}
            <!-- P0 wrap-up: cost + top_errors aggregation -->
            ${(data.top_errors || []).length > 0 ? html`<${TopErrorsTable} errors=${data.top_errors} />` : null}
          `}
      </div>
    </section>
  `;
}
