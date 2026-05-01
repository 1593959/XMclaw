// XMclaw — EvolutionPage v2 (Epic #24 Phase 3.4).
//
// Replaces the Phase 1 placeholder. Backed entirely by /api/v2/events
// — no new endpoint needed. Three sections:
//
//   1. **Today summary**: counts of skill_candidate_proposed,
//      skill_promoted, skill_rolled_back + average grader score over
//      the last 50 verdicts.
//   2. **Pending proposals**: the SkillDreamCycle output. Each row
//      shows the draft skill_id, source_pattern, evidence, and
//      confidence — plus a `xmclaw evolve approve <id>` hint string
//      (the actual approval flow is CLI-driven so anti-req #12's
//      evidence gate stays at the SkillRegistry door).
//   3. **Recent grader verdicts**: a small histogram of the last 50
//      verdict scores, plus a list of the lowest-scoring tool calls
//      (those are where evolution has the most signal to act on).
//
// All queries are cached for 30s in `store.evolution.cache` so
// switching pages doesn't hammer events.db on every render.

const { h } = window.__xmc.preact;
const { useEffect, useMemo, useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";

const DAY_MS = 24 * 60 * 60 * 1000;

function fmtIsoLocal(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function Histogram({ values, bucketCount = 10 }) {
  if (!values.length) {
    return html`<div style="opacity:.6;font-size:.85rem">尚无 grader 数据</div>`;
  }
  const buckets = new Array(bucketCount).fill(0);
  for (const v of values) {
    const c = Math.min(bucketCount - 1, Math.max(0, Math.floor(v * bucketCount)));
    buckets[c]++;
  }
  const max = Math.max(...buckets, 1);
  return html`
    <div style="display:flex;gap:2px;align-items:flex-end;height:48px;border-bottom:1px solid var(--color-border)">
      ${buckets.map((n, i) => {
        const pct = (n / max) * 100;
        const tone = i < bucketCount * 0.3 ? "danger"
                  : i < bucketCount * 0.7 ? "warning" : "success";
        return html`
          <div
            key=${i}
            title=${`score ${(i / bucketCount).toFixed(1)}-${((i + 1) / bucketCount).toFixed(1)} : ${n} verdicts`}
            style=${`flex:1;height:${pct}%;background:var(--xmc-${tone}, currentColor);opacity:.7`}
          ></div>
        `;
      })}
    </div>
    <div style="display:flex;justify-content:space-between;font-size:.7rem;opacity:.6;margin-top:4px">
      <span>0.0 (差)</span>
      <span>${values.length} 次</span>
      <span>1.0 (好)</span>
    </div>
  `;
}


function ProposalCard({ ev }) {
  const p = ev.payload || {};
  const draft = p.draft || {};
  const evidence = (p.evidence || []).slice(0, 5);
  const ts = ev.ts;
  const conf = draft.confidence;
  return html`
    <div class="xmc-h-skill-card" style="padding:.6rem .8rem">
      <div style="display:flex;align-items:baseline;gap:.5rem;flex-wrap:wrap;margin-bottom:.4rem">
        <code class="xmc-h-skill-card__id">${p.winner_candidate_id || "?"}</code>
        ${p.decision === "rollback"
          ? html`<span class="xmc-h-badge xmc-h-badge--warning">回滚提议</span>`
          : html`<span class="xmc-h-badge xmc-h-badge--info">新技能提议</span>`}
        ${conf != null
          ? html`<span class="xmc-h-badge xmc-h-badge--muted">conf ${(conf).toFixed(2)}</span>`
          : null}
        <small style="margin-left:auto;opacity:.6">${fmtIsoLocal(ts)}</small>
      </div>
      ${draft.title
        ? html`<div style="font-weight:600;margin-bottom:.2rem">${draft.title}</div>`
        : null}
      ${draft.description
        ? html`<small style="display:block;color:var(--xmc-fg-muted);margin-bottom:.4rem">${draft.description}</small>`
        : null}
      ${draft.body
        ? html`<pre style="font-size:.72rem;background:color-mix(in srgb, var(--midground) 6%, transparent);padding:.4rem .6rem;border-radius:4px;overflow-x:auto;margin:.2rem 0;white-space:pre-wrap">${draft.body.slice(0, 600)}${draft.body.length > 600 ? "…" : ""}</pre>`
        : null}
      ${p.reason
        ? html`<small style="display:block;font-style:italic;opacity:.7;margin-bottom:.3rem">来源模式：${p.reason}</small>`
        : null}
      ${evidence.length
        ? html`<div style="font-size:.7rem;opacity:.7">
            evidence: ${evidence.map((e) => html`<code style="font-size:.65rem;margin-right:.3rem" key=${e}>${e}</code>`)}
          </div>`
        : null}
      <div style="margin-top:.5rem;font-size:.72rem;opacity:.7">
        审批：终端跑 <code>xmclaw evolve approve ${p.winner_candidate_id || "&lt;id&gt;"}</code>
      </div>
    </div>
  `;
}

function MutationRow({ ev }) {
  const p = ev.payload || {};
  const isPromote = ev.type === "skill_promoted";
  return html`
    <li style="display:flex;gap:.5rem;align-items:baseline;padding:.3rem .5rem;border-bottom:1px solid var(--color-border)">
      <span class=${"xmc-h-badge xmc-h-badge--" + (isPromote ? "success" : "warning")}>
        ${isPromote ? "promote" : "rollback"}
      </span>
      <code style="font-size:.78rem">${p.skill_id || "?"}</code>
      <small style="opacity:.7">v${p.from_version ?? "?"} → v${p.to_version ?? "?"}</small>
      ${p.reason
        ? html`<small style="opacity:.6;font-style:italic">${p.reason.slice(0, 80)}</small>`
        : null}
      <small style="margin-left:auto;opacity:.6">${fmtIsoLocal(ev.ts)}</small>
    </li>
  `;
}


export function EvolutionPage({ token }) {
  const [proposals, setProposals] = useState(null);
  const [verdicts, setVerdicts] = useState(null);
  const [mutations, setMutations] = useState(null);
  const [error, setError] = useState(null);

  const load = () => {
    if (!token) return;  // wait for store hydration; useEffect re-fires on token change
    const since = (Date.now() - 7 * DAY_MS) / 1000;
    Promise.all([
      apiGet(`/api/v2/events?types=skill_candidate_proposed&since=${since}&limit=50`, token),
      apiGet(`/api/v2/events?types=grader_verdict&limit=50`, token),
      apiGet(`/api/v2/events?types=skill_promoted,skill_rolled_back&since=${since}&limit=20`, token),
    ]).then(([p, g, m]) => {
      // Clear any prior error so a transient 401 during token hydration
      // doesn't stick on screen forever.
      setError(null);
      setProposals(p.events || []);
      setVerdicts(g.events || []);
      setMutations(m.events || []);
    }).catch((e) => setError(String(e.message || e)));
  };

  useEffect(() => {
    if (!token) return;
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, [token]);

  const summary = useMemo(() => {
    if (!proposals || !verdicts || !mutations) return null;
    const scores = verdicts
      .map((e) => e.payload?.score)
      .filter((s) => typeof s === "number");
    const avg = scores.length
      ? scores.reduce((a, b) => a + b, 0) / scores.length
      : null;
    return {
      proposalCount: proposals.length,
      promoteCount: mutations.filter((e) => e.type === "skill_promoted").length,
      rollbackCount: mutations.filter((e) => e.type === "skill_rolled_back").length,
      verdictCount: scores.length,
      gradeAvg: avg,
    };
  }, [proposals, verdicts, mutations]);

  if (error) {
    return html`
      <section class="xmc-h-page" aria-labelledby="evo-title">
        <header class="xmc-h-page__header">
          <h2 id="evo-title" class="xmc-h-page__title">进化</h2>
        </header>
        <div class="xmc-h-page__body"><div class="xmc-h-error">${error}</div></div>
      </section>
    `;
  }

  if (proposals === null) {
    return html`
      <section class="xmc-h-page" aria-labelledby="evo-title">
        <header class="xmc-h-page__header">
          <h2 id="evo-title" class="xmc-h-page__title">进化</h2>
        </header>
        <div class="xmc-h-page__body"><div class="xmc-h-loading">载入中…</div></div>
      </section>
    `;
  }

  const verdictScores = (verdicts || [])
    .map((e) => e.payload?.score)
    .filter((s) => typeof s === "number");

  return html`
    <section class="xmc-h-page" aria-labelledby="evo-title">
      <header class="xmc-h-page__header">
        <div class="xmc-h-page__heading">
          <h2 id="evo-title" class="xmc-h-page__title">进化</h2>
          <p class="xmc-h-page__subtitle">
            HonestGrader · EvolutionAgent observer · SkillDreamCycle 三件套的可视面板。
            候选只提议，approve 走 <code>xmclaw evolve approve &lt;id&gt;</code> CLI（anti-req #12 在 SkillRegistry 入口强制）。
          </p>
        </div>
      </header>

      <div class="xmc-h-page__body" style="display:grid;gap:1rem;max-width:1100px">

        <!-- Section 1: Summary cards -->
        <div style="display:grid;gap:.6rem;grid-template-columns:repeat(auto-fit,minmax(160px,1fr))">
          <div class="xmc-h-skill-card" style="padding:.7rem .8rem">
            <small style="opacity:.7">7 天内提议</small>
            <div style="font-size:1.6rem;font-weight:600">${summary?.proposalCount ?? 0}</div>
          </div>
          <div class="xmc-h-skill-card" style="padding:.7rem .8rem">
            <small style="opacity:.7">7 天内 promote</small>
            <div style="font-size:1.6rem;font-weight:600;color:var(--xmc-success, currentColor)">
              ${summary?.promoteCount ?? 0}
            </div>
          </div>
          <div class="xmc-h-skill-card" style="padding:.7rem .8rem">
            <small style="opacity:.7">7 天内 rollback</small>
            <div style="font-size:1.6rem;font-weight:600;color:var(--xmc-warning, currentColor)">
              ${summary?.rollbackCount ?? 0}
            </div>
          </div>
          <div class="xmc-h-skill-card" style="padding:.7rem .8rem">
            <small style="opacity:.7">最近 50 个 grader 平均</small>
            <div style="font-size:1.6rem;font-weight:600">
              ${summary?.gradeAvg != null ? summary.gradeAvg.toFixed(2) : "—"}
            </div>
            <small style="opacity:.6">${summary?.verdictCount ?? 0} verdicts</small>
          </div>
        </div>

        <!-- Section 2: Pending proposals -->
        <div>
          <h3 style="margin:0 0 .5rem;font-size:1.05rem">待审进化提议</h3>
          ${proposals.length === 0
            ? html`<div class="xmc-h-empty" style="padding:1rem;font-size:.85rem;line-height:1.6">
                <p style="margin:0 0 .4rem"><strong>暂无待审提议。</strong></p>
                <p style="margin:0 0 .3rem;opacity:.8">B-164 起进化是<strong>实时</strong>的：
                每轮对话结束 ~15s 后 SkillProposer 会扫最近的 journal，
                找重复出现的 tool 模式，让 LLM 起草新 SKILL.md 候选。
                还会兜底跑 30 分钟周期任务以防漏掉空闲时段的演化机会。</p>
                <p style="margin:0;opacity:.7;font-size:.78rem">想关实时触发？<code>daemon/config.json</code> 加
                <code>"evolution":{"realtime":{"enabled":false}}</code>。</p>
              </div>`
            : html`<div style="display:grid;gap:.5rem">
                ${proposals.map((ev) => html`<${ProposalCard} key=${ev.id} ev=${ev} />`)}
              </div>`}
        </div>

        <!-- Section 3: Grader histogram -->
        <div>
          <h3 style="margin:0 0 .5rem;font-size:1.05rem">最近 grader 评分分布</h3>
          <${Histogram} values=${verdictScores} />
        </div>

        <!-- Section 4: Recent mutations -->
        <div>
          <h3 style="margin:0 0 .5rem;font-size:1.05rem">最近 7 天 promote/rollback 时间线</h3>
          ${(mutations || []).length === 0
            ? html`<div style="opacity:.6;font-size:.85rem;padding:.5rem">尚无版本变动</div>`
            : html`<ul style="list-style:none;padding:0;margin:0">
                ${(mutations || []).map((ev) => html`<${MutationRow} key=${ev.id} ev=${ev} />`)}
              </ul>`}
        </div>

      </div>
    </section>
  `;
}
