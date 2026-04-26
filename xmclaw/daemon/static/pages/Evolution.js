// XMclaw — Evolution page
//
// Same data source as Skills (skill_promoted / candidate / rolled_back) but
// framed as "what changed today" — counts, a 7-day activity sparkline, and
// the recent feed.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet } from "../lib/api.js";

const TYPES = "skill_promoted,skill_rolled_back,skill_candidate_proposed";

export function EvolutionPage({ token }) {
  const [events, setEvents] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet(`/api/v2/events?limit=200&types=${TYPES}`, token)
      .then((d) => { if (!cancelled) setEvents(d.events || []); })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [token]);

  if (error) return html`<section class="xmc-datapage"><h2>进化</h2><p class="xmc-datapage__error">${error}</p></section>`;
  if (!events) return html`<section class="xmc-datapage"><p>加载中…</p></section>`;

  const now = Date.now() / 1000;
  const dayAgo = now - 86400;
  const weekAgo = now - 7 * 86400;
  const today = events.filter((e) => e.ts >= dayAgo);
  const week = events.filter((e) => e.ts >= weekAgo);
  const promoted = today.filter((e) => e.type === "skill_promoted").length;
  const rolledBack = today.filter((e) => e.type === "skill_rolled_back").length;
  const candidates = today.filter((e) => e.type === "skill_candidate_proposed").length;

  // 7-day events-per-day buckets (oldest left, today right). Inline SVG
  // sparkline keeps the dep tree zero-cost — no chart lib pulled in.
  const buckets = new Array(7).fill(0);
  for (const e of week) {
    const ageDays = Math.floor((now - e.ts) / 86400);
    const idx = 6 - Math.min(6, Math.max(0, ageDays));
    buckets[idx] += 1;
  }
  const max = Math.max(1, ...buckets);
  const W = 280, H = 60, PAD = 4;
  const stepX = (W - PAD * 2) / (buckets.length - 1);
  const points = buckets.map((v, i) => {
    const x = PAD + i * stepX;
    const y = H - PAD - (v / max) * (H - PAD * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");

  return html`
    <section class="xmc-datapage" aria-labelledby="evo-title">
      <header class="xmc-datapage__header">
        <h2 id="evo-title">进化 ★</h2>
        <p class="xmc-datapage__subtitle">
          基于 EvolutionOrchestrator 事件流的近况 — 计数、7 天活动趋势、最近事件。
        </p>
      </header>
      <div style="display:flex;gap:.75rem;margin-bottom:1rem;flex-wrap:wrap">
        <div class="xmc-datapage__row" style="flex:1;min-width:120px">
          <small>今日晋升</small>
          <strong style="font-size:1.5rem">${promoted}</strong>
        </div>
        <div class="xmc-datapage__row" style="flex:1;min-width:120px">
          <small>今日回滚</small>
          <strong style="font-size:1.5rem">${rolledBack}</strong>
        </div>
        <div class="xmc-datapage__row" style="flex:1;min-width:120px">
          <small>今日候选</small>
          <strong style="font-size:1.5rem">${candidates}</strong>
        </div>
        <div class="xmc-datapage__row" style="flex:1;min-width:120px">
          <small>近 7 天事件</small>
          <strong style="font-size:1.5rem">${week.length}</strong>
        </div>
      </div>
      <h3 style="margin:1rem 0 .5rem">7 天活动趋势</h3>
      <div class="xmc-datapage__row" style="padding:.75rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap">
        <svg viewBox="0 0 ${W} ${H}" width=${W} height=${H} style="display:block">
          <polyline
            fill="none"
            stroke="var(--xmc-accent)"
            stroke-width="2"
            points=${points}
          />
          ${buckets.map((v, i) => {
            const x = PAD + i * stepX;
            const y = H - PAD - (v / max) * (H - PAD * 2);
            return html`<circle key=${i} cx=${x.toFixed(1)} cy=${y.toFixed(1)} r="2.5" fill="var(--xmc-accent)" />`;
          })}
        </svg>
        <small style="color:var(--xmc-fg-muted);font-family:var(--xmc-font-mono)">
          每日: ${buckets.join(" / ")}（peak ${max}）
        </small>
      </div>

      <h3 style="margin:1rem 0 .5rem">最近活动</h3>
      ${events.length === 0
        ? html`<p class="xmc-datapage__empty">还没有进化事件</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${events.slice(-30).reverse().map((e) => {
                const ts = e.ts ? new Date(e.ts * 1000).toLocaleString() : "";
                return html`
                  <li class="xmc-datapage__row" key=${e.id || ts}>
                    <div style="display:flex;justify-content:space-between;gap:.5rem">
                      <${Badge} tone="muted">${e.type}</${Badge}>
                      <small>${ts}</small>
                    </div>
                    <code>${JSON.stringify(e.payload).slice(0, 140)}</code>
                  </li>
                `;
              })}
            </ul>
          `}
    </section>
  `;
}
