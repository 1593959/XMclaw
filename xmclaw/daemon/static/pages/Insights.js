// XMclaw — Insights page
//
// Latest events from the SqliteEventBus log (Epic #13). Useful for cost,
// tool-usage, and skill-evolution visibility without leaving the UI.
// We pull the last 50 events; users wanting deep search should use the
// CLI's `xmclaw events --q` for now.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet } from "../lib/api.js";

const TONE_BY_TYPE = {
  llm_response: "muted",
  llm_request: "muted",
  tool_call_emitted: "muted",
  tool_invocation_finished: "muted",
  user_message: "muted",
  cost_tick: "warn",
  anti_req_violation: "error",
  skill_promoted: "success",
  skill_rolled_back: "warn",
  session_lifecycle: "muted",
};

export function InsightsPage({ token }) {
  const [events, setEvents] = useState(null);
  const [error, setError] = useState(null);
  const [bus, setBus] = useState("");

  async function load(signal) {
    try {
      const d = await apiGet("/api/v2/events?limit=50", token);
      if (signal && signal.cancelled) return;
      setEvents(d.events || []);
      setBus(d.bus || "");
      setError(null);
    } catch (exc) {
      if (signal && signal.cancelled) return;
      setError(String(exc.message || exc));
    }
  }

  useEffect(() => {
    const signal = { cancelled: false };
    load(signal);
    const id = setInterval(() => load(signal), 6000);
    return () => { signal.cancelled = true; clearInterval(id); };
  }, [token]);

  if (error) return html`<section class="xmc-datapage"><h2>洞察</h2><p class="xmc-datapage__error">${error}</p></section>`;
  if (!events) return html`<section class="xmc-datapage"><p>加载中…</p></section>`;

  return html`
    <section class="xmc-datapage" aria-labelledby="ins-title">
      <header class="xmc-datapage__header">
        <h2 id="ins-title">洞察</h2>
        <p class="xmc-datapage__subtitle">
          最近 ${events.length} 条事件 · bus=<code>${bus}</code> · 每 6 秒刷新
        </p>
      </header>
      ${events.length === 0
        ? html`<p class="xmc-datapage__empty">还没有事件落盘</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${events.slice().reverse().map((e) => {
                const t = e.type || "?";
                const ts = e.ts ? new Date(e.ts * 1000).toLocaleTimeString() : "";
                const summary = JSON.stringify(e.payload || {}).slice(0, 140);
                return html`
                  <li class="xmc-datapage__row" key=${e.id || `${ts}-${t}`}>
                    <div style="display:flex;justify-content:space-between;gap:.5rem">
                      <${Badge} tone=${TONE_BY_TYPE[t] || "muted"}>${t}</${Badge}>
                      <small>${ts}</small>
                    </div>
                    <code>${summary}</code>
                  </li>
                `;
              })}
            </ul>
          `}
    </section>
  `;
}
