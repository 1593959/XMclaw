// XMclaw — Skills page
//
// Two sections:
//   1. 已注册技能 — pulled from /api/v2/skills (registry contents).
//      Each skill row shows source badge ("内置" / "用户安装"), HEAD
//      version, all registered versions. Mirrors the Hermes peer-pattern
//      of trust-tier tagging so the user can see at a glance what came
//      with the wheel vs what they (or evolution) installed.
//
//   2. 进化事件 — skill_promoted / skill_rolled_back / skill_candidate_proposed
//      from /api/v2/events. The same flashes the CLI shows in green.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet } from "../lib/api.js";

const EVENT_TYPES = "skill_promoted,skill_rolled_back,skill_candidate_proposed";

function SourceBadge({ source }) {
  if (source === "built-in") return html`<${Badge} tone="muted">内置</${Badge}>`;
  if (source === "user")     return html`<${Badge} tone="success">用户安装</${Badge}>`;
  return html`<${Badge} tone="muted">未知</${Badge}>`;
}

export function SkillsPage({ token }) {
  const [skills, setSkills] = useState(null);
  const [skillsErr, setSkillsErr] = useState(null);
  const [evolutionEnabled, setEvolutionEnabled] = useState(true);
  const [events, setEvents] = useState(null);
  const [eventsErr, setEventsErr] = useState(null);

  async function load(signal) {
    try {
      const s = await apiGet("/api/v2/skills", token);
      if (signal.cancelled) return;
      setSkills(s.skills || []);
      setEvolutionEnabled(s.evolution_enabled !== false);
      setSkillsErr(null);
    } catch (exc) {
      if (signal.cancelled) return;
      setSkillsErr(String(exc.message || exc));
    }
    try {
      const d = await apiGet(`/api/v2/events?limit=50&types=${EVENT_TYPES}`, token);
      if (signal.cancelled) return;
      setEvents(d.events || []);
      setEventsErr(null);
    } catch (exc) {
      if (signal.cancelled) return;
      setEventsErr(String(exc.message || exc));
    }
  }

  useEffect(() => {
    const signal = { cancelled: false };
    load(signal);
    const id = setInterval(() => load(signal), 8000);
    return () => { signal.cancelled = true; clearInterval(id); };
  }, [token]);

  const builtIn = (skills || []).filter((s) => s.source === "built-in");
  const userInstalled = (skills || []).filter((s) => s.source !== "built-in");

  function SkillSection({ title, items, hint }) {
    return html`
      <section class="xmc-skills__section">
        <h3>${title} <small>(${items.length})</small></h3>
        ${items.length === 0
          ? html`<p class="xmc-datapage__empty">${hint}</p>`
          : html`
              <ul class="xmc-datapage__list">
                ${items.map((s) => html`
                  <li class="xmc-datapage__row" key=${s.id}>
                    <div style="display:flex;justify-content:space-between;gap:.5rem;align-items:center">
                      <strong>${s.id}</strong>
                      <${SourceBadge} source=${s.source} />
                    </div>
                    <small>HEAD: v${s.head_version ?? "—"} · 共 ${s.versions.length} 个版本</small>
                    ${s.versions.length > 1 ? html`
                      <small>版本: ${s.versions.map((v) => v.is_head ? `[v${v.version}]` : `v${v.version}`).join(", ")}</small>
                    ` : null}
                  </li>
                `)}
              </ul>
            `}
      </section>
    `;
  }

  return html`
    <section class="xmc-datapage" aria-labelledby="skills-title">
      <header class="xmc-datapage__header">
        <h2 id="skills-title">技能</h2>
        <p class="xmc-datapage__subtitle">
          ${evolutionEnabled
            ? "来源分类（内置 = 随包自带；用户安装 = 运行时注册或晋升）+ 进化事件流。"
            : "演化未启用 — 在 daemon/config.json 设置 evolution.enabled=true 解锁进化。"}
        </p>
      </header>

      ${skillsErr
        ? html`<p class="xmc-datapage__error">技能列表错误: ${skillsErr}</p>`
        : null}

      ${skills == null
        ? html`<p>加载中…</p>`
        : html`
          <${SkillSection}
            title="内置技能"
            items=${builtIn}
            hint="尚无内置技能注册 — demo 技能不会自动注册" />
          <${SkillSection}
            title="用户安装 / 晋升技能"
            items=${userInstalled}
            hint="尚无用户技能 — 让 evolution 跑出候选并晋升后会出现在这里" />
        `}

      <section class="xmc-skills__section">
        <h3>进化事件 <small>(最近 50 条)</small></h3>
        ${eventsErr ? html`<p class="xmc-datapage__error">${eventsErr}</p>` : null}
        ${events == null && !eventsErr ? html`<p>加载中…</p>` : null}
        ${events && events.length === 0
          ? html`<p class="xmc-datapage__empty">尚无技能事件 — 让 agent 多跑几轮，evolution 才会出候选</p>`
          : null}
        ${events && events.length > 0 ? html`
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
        ` : null}
      </section>
    </section>
  `;
}
