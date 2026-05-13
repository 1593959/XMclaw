// XMclaw — Dashboard page (Sprint 2 Wave 6)
//
// "Control tower" view that pulls a single overview snapshot from
// /api/v2/dashboard/overview every 10s and renders cards for:
//   - Daemon uptime + version
//   - Proactive triggers (registered + last fire + quiet hours)
//   - Autobiographical memory (people / projects counts + recent)
//   - Cognitive state (goals + attention focus)
//   - Pending suggestions
//   - Task scheduler queue
//   - Storage footprint (DB file sizes)
//
// Every block is best-effort on the server — if a subsystem isn't
// wired, the card renders a "未启用" placeholder instead of erroring.

const { h } = window.__xmc.preact;
const { useEffect, useState, useRef } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { Skeleton } from "../components/atoms/skeleton.js";

const REFRESH_INTERVAL_MS = 10_000;

function fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fmtDuration(s) {
  if (s == null) return "—";
  const sec = Math.max(0, Math.round(s));
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ${sec % 60}s`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ${min % 60}m`;
  const day = Math.floor(hr / 24);
  return `${day}d ${hr % 24}h`;
}

function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function Card({ title, hint, children }) {
  return html`
    <div class="xmc-dash__card">
      <header class="xmc-dash__card-head">
        <h2 class="xmc-dash__card-title">${title}</h2>
        ${hint ? html`<span class="xmc-dash__card-hint">${hint}</span>` : null}
      </header>
      <div class="xmc-dash__card-body">${children}</div>
    </div>
  `;
}

function Stat({ label, value }) {
  return html`
    <div class="xmc-dash__stat">
      <div class="xmc-dash__stat-label">${label}</div>
      <div class="xmc-dash__stat-value">${value}</div>
    </div>
  `;
}

function EmptyHint({ text }) {
  return html`<div class="xmc-dash__empty">${text}</div>`;
}

// ── Cards ─────────────────────────────────────────────────────────

function UptimeCard({ uptime }) {
  if (!uptime) return null;
  return html`
    <${Card} title="守护进程" hint=${uptime.version ? `v${uptime.version}` : ""}>
      <div class="xmc-dash__stats-grid">
        <${Stat} label="运行时长" value=${fmtDuration(uptime.uptime_s)} />
        <${Stat} label="启动耗时" value=${uptime.startup_duration_s != null ? `${uptime.startup_duration_s}s` : "—"} />
        <${Stat} label="启动时刻" value=${fmtTs(uptime.boot_ts)} />
      </div>
    </${Card}>
  `;
}

function ProactiveCard({ proactive, now }) {
  if (proactive == null) {
    return html`
      <${Card} title="主动认知">
        <${EmptyHint} text="未启用 — 在 daemon/config.json 中开启 proactive.enabled" />
      </${Card}>
    `;
  }
  if (proactive.error) {
    return html`<${Card} title="主动认知"><div class="xmc-dash__err">读取失败：${proactive.error}</div></${Card}>`;
  }
  const last = proactive.last_proposal_ts
    ? fmtDuration(now - proactive.last_proposal_ts) + " 前"
    : "尚无";
  return html`
    <${Card} title="主动认知" hint=${`${proactive.tick_interval_s}s · 一周期`}>
      <div class="xmc-dash__stats-grid">
        <${Stat} label="已注册触发器" value=${proactive.triggers.length} />
        <${Stat} label="上次发声" value=${last} />
        <${Stat} label="勿扰模式" value=${proactive.quiet_hours_active ? "开启中" : "关闭"} />
      </div>
      ${proactive.triggers.length > 0 ? html`
        <ul class="xmc-dash__list">
          ${proactive.triggers.map((t) => html`
            <li class="xmc-dash__list-item">
              <span class="xmc-dash__tag">${t.name}</span>
              <span class="xmc-dash__list-meta">冷却 ${fmtDuration(t.cooldown_s)}</span>
            </li>
          `)}
        </ul>
      ` : null}
    </${Card}>
  `;
}

function AutobioCard({ autobio }) {
  if (autobio == null) {
    return html`
      <${Card} title="自传式记忆">
        <${EmptyHint} text="未启用 — 配置 autobio_memory" />
      </${Card}>
    `;
  }
  if (autobio.error) {
    return html`<${Card} title="自传式记忆"><div class="xmc-dash__err">读取失败：${autobio.error}</div></${Card}>`;
  }
  return html`
    <${Card} title="自传式记忆" hint="agent 记得的人和事">
      <div class="xmc-dash__stats-grid">
        <${Stat} label="认识的人" value=${autobio.people_count} />
        <${Stat} label="进行中项目" value=${autobio.project_count} />
      </div>
      ${autobio.recent_people.length > 0 ? html`
        <div class="xmc-dash__subhead">最近遇到的人</div>
        <ul class="xmc-dash__list">
          ${autobio.recent_people.map((p) => html`
            <li class="xmc-dash__list-item">
              <span class="xmc-dash__tag">${p.relationship || "—"}</span>
              <strong>${p.name}</strong>
              <span class="xmc-dash__list-meta">重要度 ${p.importance}</span>
            </li>
          `)}
        </ul>
      ` : null}
      ${autobio.recent_projects.length > 0 ? html`
        <div class="xmc-dash__subhead">项目动态</div>
        <ul class="xmc-dash__list">
          ${autobio.recent_projects.map((p) => html`
            <li class="xmc-dash__list-item">
              <span class="xmc-dash__tag">${p.status || "active"}</span>
              <strong>${p.name}</strong>
              ${p.current_focus ? html`<span class="xmc-dash__list-meta">${p.current_focus}</span>` : null}
              ${p.last_touch_ts ? html`<span class="xmc-dash__list-meta">${fmtTs(p.last_touch_ts)}</span>` : null}
            </li>
          `)}
        </ul>
      ` : null}
    </${Card}>
  `;
}

function CognitionCard({ cognition }) {
  if (cognition == null) {
    return html`
      <${Card} title="认知状态">
        <${EmptyHint} text="未启用 — 开启 cognition.enabled" />
      </${Card}>
    `;
  }
  if (cognition.error) {
    return html`<${Card} title="认知状态"><div class="xmc-dash__err">读取失败：${cognition.error}</div></${Card}>`;
  }
  return html`
    <${Card} title="认知状态">
      <div class="xmc-dash__stats-grid">
        <${Stat} label="活跃目标" value=${cognition.goal_count} />
        <${Stat} label="注意力焦点" value=${cognition.attention_count} />
        <${Stat} label="显著阈值" value=${cognition.salience_threshold != null ? cognition.salience_threshold.toFixed(2) : "—"} />
      </div>
      ${cognition.active_goals.length > 0 ? html`
        <div class="xmc-dash__subhead">目标列表</div>
        <ul class="xmc-dash__list">
          ${cognition.active_goals.map((g) => html`
            <li class="xmc-dash__list-item">
              <span class="xmc-dash__tag">P${g.priority}</span>
              <span>${g.description}</span>
              <span class="xmc-dash__list-meta">${g.status}</span>
            </li>
          `)}
        </ul>
      ` : null}
    </${Card}>
  `;
}

function SuggestionsCard({ suggestions, now }) {
  if (suggestions == null) {
    return html`
      <${Card} title="待审建议">
        <${EmptyHint} text="未启用 — 开启 cognition.continuous_loop 才出 inbox" />
      </${Card}>
    `;
  }
  if (suggestions.error) {
    return html`<${Card} title="待审建议"><div class="xmc-dash__err">读取失败：${suggestions.error}</div></${Card}>`;
  }
  return html`
    <${Card} title="待审建议">
      <div class="xmc-dash__stats-grid">
        <${Stat} label="待审数量" value=${suggestions.pending_count} />
      </div>
      ${suggestions.recent.length > 0 ? html`
        <ul class="xmc-dash__list">
          ${suggestions.recent.map((s) => html`
            <li class="xmc-dash__list-item">
              <span class="xmc-dash__tag">${s.urgency}</span>
              <span>${s.text}</span>
              ${s.created_ts ? html`<span class="xmc-dash__list-meta">${fmtDuration(now - s.created_ts)} 前</span>` : null}
            </li>
          `)}
        </ul>
      ` : html`<${EmptyHint} text="没有待审建议 — agent 没有想跟你说的话" />`}
    </${Card}>
  `;
}

function TasksCard({ tasks }) {
  if (tasks == null) {
    return html`
      <${Card} title="任务队列">
        <${EmptyHint} text="未启用 — task_scheduler 未配置" />
      </${Card}>
    `;
  }
  if (tasks.error) {
    return html`<${Card} title="任务队列"><div class="xmc-dash__err">读取失败：${tasks.error}</div></${Card}>`;
  }
  const statuses = Object.entries(tasks.by_status || {});
  return html`
    <${Card} title="任务队列">
      <div class="xmc-dash__stats-grid">
        <${Stat} label="总数" value=${tasks.total} />
        ${statuses.slice(0, 3).map(([k, v]) => html`<${Stat} label=${k} value=${v} />`)}
      </div>
    </${Card}>
  `;
}

function StorageCard({ storage }) {
  if (!storage) return null;
  return html`
    <${Card} title="本地存储" hint=${storage.data_dir || ""}>
      <div class="xmc-dash__stats-grid">
        <${Stat} label="事件库" value=${fmtBytes(storage.events_db_bytes)} />
        <${Stat} label="记忆库" value=${fmtBytes(storage.memory_db_bytes)} />
        <${Stat} label="自传库" value=${fmtBytes(storage.autobio_db_bytes)} />
      </div>
    </${Card}>
  `;
}

function CostTodayCard({ cost }) {
  if (cost == null) {
    return html`
      <${Card} title="今日花费">
        <${EmptyHint} text="事件总线未启用 — 无法查询过去 24h 调用" />
      </${Card}>
    `;
  }
  if (cost.error) {
    return html`<${Card} title="今日花费"><div class="xmc-dash__err">读取失败：${cost.error}</div></${Card}>`;
  }
  const total = (cost.total_usd ?? 0).toFixed(4);
  const tokensTotal = (cost.prompt_tokens || 0) + (cost.completion_tokens || 0);
  return html`
    <${Card} title="今日花费" hint=${`过去 24h · ${cost.call_count} 次调用`}>
      <div class="xmc-dash__stats-grid">
        <${Stat} label="美元" value=${`$${total}`} />
        <${Stat} label="Token" value=${tokensTotal.toLocaleString()} />
        ${cost.cache_hit_rate != null ? html`
          <${Stat} label="缓存命中" value=${`${(cost.cache_hit_rate * 100).toFixed(1)}%`} />
        ` : null}
      </div>
      ${cost.by_model.length > 0 ? html`
        <div class="xmc-dash__subhead">按模型</div>
        <ul class="xmc-dash__list">
          ${cost.by_model.map((row) => html`
            <li class="xmc-dash__list-item">
              <span class="xmc-dash__tag">${row.calls}×</span>
              <span>${row.model}</span>
              <span class="xmc-dash__list-meta">$${row.cost_usd.toFixed(4)}</span>
            </li>
          `)}
        </ul>
      ` : null}
    </${Card}>
  `;
}

const EVENT_ICONS = {
  proactive_proposal:     "📢",
  reflection_cycle_ran:   "🪞",
  memory_consolidated:    "🧠",
  goals_groomed:          "🎯",
  metacognition_proposal: "💡",
  task_state_changed:     "🔄",
  evolution_promoted:     "⬆",
};

function RecentEventsCard({ recentEvents, now }) {
  // Span the full grid width — this is the "what was the agent doing"
  // timeline and benefits from one long column over multiple short ones.
  if (recentEvents == null) {
    return html`
      <${Card} title="最近活动">
        <${EmptyHint} text="事件总线未启用 — 在内存总线下无持久化记录" />
      </${Card}>
    `;
  }
  if (recentEvents.length > 0 && recentEvents[0].error) {
    return html`<${Card} title="最近活动"><div class="xmc-dash__err">读取失败：${recentEvents[0].error}</div></${Card}>`;
  }
  if (recentEvents.length === 0) {
    return html`
      <${Card} title="最近活动" hint="近 25 条主动认知事件">
        <${EmptyHint} text="还没记录到 agent 自己发起的活动" />
      </${Card}>
    `;
  }
  return html`
    <div class="xmc-dash__card xmc-dash__card--wide">
      <header class="xmc-dash__card-head">
        <h2 class="xmc-dash__card-title">最近活动</h2>
        <span class="xmc-dash__card-hint">近 ${recentEvents.length} 条主动认知事件</span>
      </header>
      <ol class="xmc-dash__timeline">
        ${recentEvents.map((e) => html`
          <li class="xmc-dash__timeline-item">
            <span class="xmc-dash__timeline-icon" aria-hidden="true">
              ${EVENT_ICONS[e.type] || "•"}
            </span>
            <span class="xmc-dash__timeline-text">${e.summary}</span>
            <span class="xmc-dash__timeline-meta">${fmtDuration(now - e.ts)} 前</span>
          </li>
        `)}
      </ol>
    </div>
  `;
}

// ── Main page ─────────────────────────────────────────────────────

export function DashboardPage({ token }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const timerRef = useRef(null);

  async function refresh() {
    try {
      const d = await apiGet("/api/v2/dashboard/overview", token);
      setData(d);
      setError(null);
    } catch (e) {
      setError(e && e.message ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!token) return;
    refresh();
    timerRef.current = setInterval(refresh, REFRESH_INTERVAL_MS);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [token]);

  if (loading) {
    return html`
      <section class="xmc-datapage xmc-dash" aria-labelledby="dashboard-title">
        <header class="xmc-datapage__header">
          <h2 id="dashboard-title">概览</h2>
          <p class="xmc-datapage__subtitle">系统运行状态一览</p>
        </header>
        <${Skeleton} height="120px" />
      </section>
    `;
  }

  if (error) {
    return html`
      <section class="xmc-datapage xmc-dash" aria-labelledby="dashboard-title">
        <header class="xmc-datapage__header">
          <h2 id="dashboard-title">概览</h2>
        </header>
        <div class="xmc-dash__err">加载失败：${error}</div>
      </section>
    `;
  }

  const now = data ? data.now : 0;
  return html`
    <section class="xmc-datapage xmc-dash" aria-labelledby="dashboard-title">
      <header class="xmc-datapage__header">
        <h2 id="dashboard-title">概览</h2>
        <p class="xmc-datapage__subtitle">每 10 秒自动刷新 · 最近一次 ${fmtTs(now)}</p>
      </header>
      <div class="xmc-dash__grid">
        <${UptimeCard} uptime=${data.uptime} />
        <${ProactiveCard} proactive=${data.proactive} now=${now} />
        <${AutobioCard} autobio=${data.autobio} />
        <${CognitionCard} cognition=${data.cognition} />
        <${SuggestionsCard} suggestions=${data.suggestions} now=${now} />
        <${TasksCard} tasks=${data.tasks} />
        <${StorageCard} storage=${data.storage} />
        <${CostTodayCard} cost=${data.cost_today} />
        <${RecentEventsCard} recentEvents=${data.recent_events} now=${now} />
      </div>
    </section>
  `;
}
