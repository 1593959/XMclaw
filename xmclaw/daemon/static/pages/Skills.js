// XMclaw — Skills page
//
// No /api/v2/skills endpoint yet — instead we filter the event stream for
// skill_promoted / skill_rolled_back / skill_candidate_proposed and surface
// the latest 50 entries. This is the same data that lights up `xmclaw chat`
// with `[evolved]` flashes, just in a stable list.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet } from "../lib/api.js";

const TYPES = "skill_promoted,skill_rolled_back,skill_candidate_proposed";

export function SkillsPage({ token }) {
  const [events, setEvents] = useState(null);
  const [error, setError] = useState(null);

  async function load(signal) {
    try {
      const d = await apiGet(`/api/v2/events?limit=50&types=${TYPES}`, token);
      if (signal && signal.cancelled) return;
      setEvents(d.events || []);
      setError(null);
    } catch (exc) {
      if (signal && signal.cancelled) return;
      setError(String(exc.message || exc));
    }
  }

  useEffect(() => {
    const signal = { cancelled: false };
    load(signal);
    const id = setInterval(() => load(signal), 8000);
    return () => { signal.cancelled = true; clearInterval(id); };
  }, [token]);

  if (error) return html`<section class="xmc-datapage"><h2>技能</h2><p class="xmc-datapage__error">${error}</p></section>`;
  if (!events) return html`<section class="xmc-datapage"><p>加载中…</p></section>`;

  return html`
    <section class="xmc-datapage" aria-labelledby="skills-title">
      <header class="xmc-datapage__header">
        <h2 id="skills-title">技能</h2>
        <p class="xmc-datapage__subtitle">
          技能晋升 / 回滚 / 候选事件（来自 EvolutionOrchestrator）。
        </p>
      </header>
      ${events.length === 0
        ? html`<p class="xmc-datapage__empty">尚无技能事件 — 让 agent 多跑几轮，evolution 才会出候选</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${events.slice().reverse().map((e) => {
                const t = e.type;
                const p = e.payload || {};
                const ts = e.ts ? new Date(e.ts * 1000).toLocaleString() : "";
                const tone = t === "skill_promoted" ? "success"
                  : t === "skill_rolled_back" ? "warn" : "muted";
                const skill = p.skill_id || p.winner_candidate_id || "?";
                const fv = p.from_version, tv = p.to_version || p.winner_version;
                return html`
                  <li class="xmc-datapage__row" key=${e.id || `${ts}-${skill}`}>
                    <div style="display:flex;justify-content:space-between;gap:.5rem">
                      <strong>${skill}</strong>
                      <${Badge} tone=${tone}>${t}</${Badge}>
                    </div>
                    <small>v${fv ?? "?"} → v${tv ?? "?"} · ${ts}</small>
                    ${p.reason ? html`<small>${p.reason}</small>` : null}
                  </li>
                `;
              })}
            </ul>
          `}
    </section>
  `;
}
