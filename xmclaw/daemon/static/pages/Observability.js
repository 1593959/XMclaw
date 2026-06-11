// XMclaw Observability Dashboard — live agent metrics.
//
// Reads the event stream to show: tool latency distribution,
// recall quality, hop count trends, and session timeline.
// Fetches from /api/v2/observability (or falls back to localStorage).

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

function StatCard({ label, value, unit, color }) {
  return html`
    <div class="stat-card" style=${{ borderLeft: `3px solid ${color || "var(--nb-accent, #4fc3f7)"}` }}>
      <div class="stat-card__label">${label}</div>
      <div class="stat-card__value">${value ?? "—"}<span class="stat-card__unit">${unit || ""}</span></div>
    </div>
  `;
}

function LatencyRow({ name, p50, p95, p99, count }) {
  return html`
    <div class="latency-row">
      <span class="latency-row__name">${name}</span>
      <span class="latency-row__stats">
        <span title="p50">${p50}ms</span>
        <span title="p95">${p95}ms</span>
        <span title="p99">${p99}ms</span>
        <span class="latency-row__count">×${count}</span>
      </span>
    </div>
  `;
}

export function ObservabilityPage({ token }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  const fetchData = useCallback(async () => {
    if (!token) { setLoading(false); return; }
    try {
      const resp = await fetch(`/api/v2/observability?token=${encodeURIComponent(token)}`);
      if (resp.ok) setData(await resp.json());
    } catch (_) {
      // Fallback: build stats from localStorage if available
      try {
        const raw = localStorage.getItem("xmc.observability");
        if (raw) setData(JSON.parse(raw));
      } catch (__) { /* ignore */ }
    }
    setLoading(false);
  }, [token]);

  useEffect(() => { fetchData(); const iv = setInterval(fetchData, 30000); return () => clearInterval(iv); }, [fetchData]);

  if (loading) return html`<div class="skeleton-card" />`;
  if (!data) return html`<div class="page-empty">No observability data yet. Start a conversation to collect metrics.</div>`;

  const tl = data.tool_latency || {};
  const recall = data.recall || {};
  const hops = data.hop_distribution || {};
  const sessions = data.sessions || {};

  return html`
    <div class="observability-page">
      <h2>📊 Observability</h2>
      <div class="stat-grid">
        <${StatCard} label="Sessions (24h)" value=${sessions.count_24h || 0} color="var(--nb-accent)" />
        <${StatCard} label="Avg Turn Time" value=${sessions.avg_turn_ms ? (sessions.avg_turn_ms / 1000).toFixed(1) : "—"} unit="s" color="var(--nb-success)" />
        <${StatCard} label="Avg Hops/Turn" value=${hops.avg ? hops.avg.toFixed(1) : "—"} color="var(--nb-warning)" />
        <${StatCard} label="Recall Hit Rate" value=${recall.hit_rate ? (recall.hit_rate * 100).toFixed(0) : "—"} unit="%" color="#9c27b0" />
      </div>

      <h3>🔧 Tool Latency Distribution</h3>
      <div class="latency-table">
        ${Object.entries(tl).slice(0, 10).map(([name, stats]) => html`
          <${LatencyRow} key=${name} name=${name} p50=${stats.p50 || 0} p95=${stats.p95 || 0} p99=${stats.p99 || 0} count=${stats.count || 0} />
        `)}
      </div>

      <h3>🧠 Recall Quality</h3>
      <div class="stat-grid stat-grid--3">
        <${StatCard} label="Avg Hits/Query" value=${recall.avg_hits ? recall.avg_hits.toFixed(1) : "—"} />
        <${StatCard} label="Avg Distance" value=${recall.avg_distance ? recall.avg_distance.toFixed(3) : "—"} />
        <${StatCard} label="Timeouts" value=${recall.timeouts || 0} color="var(--nb-error)" />
      </div>
    </div>
  `;
}
