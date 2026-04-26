// XMclaw — Evolution page
//
// Same data source as Skills (skill_promoted / candidate / rolled_back) but
// framed as "what changed today" — counts + the last week of activity. Real
// VFM-chart visualization will land in Phase 4 when we have the time-series
// data exposed; until then this is the human-readable feed.

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

  return html`
    <section class="xmc-datapage" aria-labelledby="evo-title">
      <header class="xmc-datapage__header">
        <h2 id="evo-title">进化 ★</h2>
        <p class="xmc-datapage__subtitle">
          基于 EvolutionOrchestrator 事件流的近况。VFM 折线图见 Phase 4。
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
