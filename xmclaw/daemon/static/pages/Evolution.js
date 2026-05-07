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
  // B-167: "propose" decisions are auto-materialized by
  // ProposalMaterializer into ~/.xmclaw/skills_user/<id>/SKILL.md +
  // SkillRegistry on receipt. The UI no longer shows an approve
  // button — by the time you read this card, the skill is already
  // active. Rollback / unregister via 技能 page.
  const isPropose = (p.decision || "propose") === "propose";
  return html`
    <div class="xmc-h-skill-card" style="padding:.6rem .8rem">
      <div style="display:flex;align-items:baseline;gap:.5rem;flex-wrap:wrap;margin-bottom:.4rem">
        <code class="xmc-h-skill-card__id">${p.winner_candidate_id || "?"}</code>
        ${p.decision === "rollback"
          ? html`<span class="xmc-h-badge xmc-h-badge--warning">回滚提议</span>`
          : html`<span class="xmc-h-badge xmc-h-badge--success">已自动激活</span>`}
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
      ${isPropose
        ? html`<div style="margin-top:.5rem;font-size:.72rem;opacity:.7">
            已写入 <code>~/.xmclaw/skills_user/${p.winner_candidate_id || "&lt;id&gt;"}/SKILL.md</code> · agent 下一轮可调用 ·
            不喜欢？去技能页 rollback / 删目录
          </div>`
        : html`<div style="margin-top:.5rem;font-size:.72rem;opacity:.7">
            回滚提议由 controller 处理（auto_apply=true 时立即生效）
          </div>`}
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


// B-301: live evolution-chain status. Renders the in-memory state
// users can otherwise only see by SSH-ing into the daemon — _arms
// per-skill progress towards min_plays / min_mean, trigger fire
// counter, variant-selector UCB1 arm count, and the most recent
// SkillDreamCycle audit lines. Polls /api/v2/evolution/snapshot
// every 30s alongside the rest of the page.
function ArmProgressBar({ progress, label }) {
  const pct = Math.round((progress || 0) * 100);
  const tone = pct >= 100 ? "success" : pct >= 50 ? "warning" : "muted";
  return html`
    <div style="display:flex;align-items:center;gap:.4rem;font-size:.72rem">
      <span style="min-width:3.5rem;opacity:.7">${label}</span>
      <div style="flex:1;height:6px;background:color-mix(in srgb, var(--midground) 12%, transparent);border-radius:3px;overflow:hidden">
        <div style=${`height:100%;width:${pct}%;background:var(--xmc-${tone}, currentColor);opacity:.7`}></div>
      </div>
      <span style="min-width:3rem;text-align:right;opacity:.7">${pct}%</span>
    </div>
  `;
}


function LiveStatusPanel({ token }) {
  const [snap, setSnap] = useState(null);
  const [error, setError] = useState(null);

  const load = () => {
    if (!token) return;
    apiGet("/api/v2/evolution/snapshot", token)
      .then((d) => { setError(null); setSnap(d); })
      .catch((e) => setError(String(e.message || e)));
  };

  useEffect(() => {
    if (!token) return;
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, [token]);

  // Loading / error / disabled-chain states all share the same outer
  // shell so the section's height stays stable across state transitions
  // (otherwise the page jumps when the snapshot resolves).
  if (error || !snap || !snap.observer) {
    let body;
    if (error) {
      body = html`<div style="color:var(--xmc-warning)">加载失败：${error}</div>`;
    } else if (!snap) {
      body = html`<div style="opacity:.6">载入中… <span style="opacity:.4">(GET /api/v2/evolution/snapshot)</span></div>`;
    } else {
      body = html`<div style="opacity:.7">
        进化链未启用（echo-mode 或 <code>evolution.enabled=false</code>）
      </div>`;
    }
    return html`
      <div class="xmc-h-skill-card" style="padding:.8rem 1rem;min-height:6rem">
        <strong style="font-size:.95rem;display:block;margin-bottom:.4rem">🔬 实时进化状态</strong>
        <div style="font-size:.85rem">${body}</div>
      </div>
    `;
  }

  const obs = snap.observer;
  const trig = snap.trigger || {};
  const sel = snap.variant_selector || {};
  const sd = snap.skill_dream || {};
  const arms = obs.arms || [];
  const recent = sd.recent_proposals || [];

  // B-301 followup: rewrote success-state with min-height + flat
  // structure (no nested ${} interpolation in template, no <br/>) so
  // a single template-parse glitch can't squash the body. The earlier
  // version used heavy nesting + <br/> and the user reported the
  // panel rendered ONLY the header strip — body content silently
  // dropped. Pre-rendering each section into local variables avoids
  // the ambiguity.

  const observerLine = obs.ready_to_propose_count > 0
    ? html`正在跟踪 ${obs.tracked_skill_count} 个 (skill_id, version) ·
        <strong style="color:var(--xmc-success)">
          ${obs.ready_to_propose_count} 个达到提议阈值
        </strong>`
    : html`正在跟踪 ${obs.tracked_skill_count} 个 (skill_id, version) ·
        <span style="opacity:.7">尚无达阈值（min_plays=10 / min_mean=0.65）</span>`;

  const triggerLine = trig.is_active
    ? html`<span style="opacity:.7">
        evaluate trigger fired ${trig.fire_count || 0} 次 ·
        ${(trig.verdicts_since_last_fire || 0) > 0
          ? `积 ${trig.verdicts_since_last_fire} 个新 verdict（阈值 ${trig.min_new_verdicts}）`
          : "本轮无新 verdict"}
      </span>`
    : null;

  let armsBlock;
  if (arms.length === 0) {
    armsBlock = html`<div style="font-size:.78rem;opacity:.6;padding:.4rem 0">
      还没有 skill_* 调用产生 grader_verdict。让 agent 实际调用一个 <code>skill_*</code> 工具，
      它就会出现在这里并开始累积 plays。
    </div>`;
  } else {
    armsBlock = html`<div style="display:grid;gap:.4rem">
      ${arms.map((a) => html`<${ArmCard} key=${`${a.skill_id}-${a.version}`} arm=${a} />`)}
    </div>`;
  }

  let dreamBlock = null;
  if (recent.length > 0) {
    dreamBlock = html`<details style="margin-top:.7rem">
      <summary style="cursor:pointer;font-size:.78rem;opacity:.8">
        SkillDreamCycle 最近 ${recent.length} 条 audit（最新在前）
      </summary>
      <ul style="list-style:none;padding:.4rem 0 0;margin:0;font-size:.74rem">
        ${recent.map((r, i) => html`<${DreamRow} key=${i} row=${r} />`)}
      </ul>
    </details>`;
  }

  return html`
    <div class="xmc-h-skill-card" style="padding:.8rem 1rem;min-height:8rem">
      <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:.6rem;flex-wrap:wrap;gap:.4rem">
        <strong style="font-size:.95rem">🔬 实时进化状态</strong>
        <small style="opacity:.6">
          observer=${obs.is_running ? "✓" : "✗"} ·
          trigger=${trig.is_active ? "✓" : "✗"} ·
          selector=${sel.is_active ? "✓" : "✗"}
        </small>
      </div>
      <div style="font-size:.82rem;line-height:1.7;margin-bottom:.6rem">
        <div><strong>EvolutionAgent observer</strong>: ${observerLine}</div>
        ${triggerLine ? html`<div>${triggerLine}</div>` : null}
      </div>
      ${armsBlock}
      ${dreamBlock}
    </div>
  `;
}


// B-301 followup: extracted from inline template so the parent's
// .map() doesn't have to embed a multi-line html template literal
// (which seemed to confuse htm's parser when the user's browser
// had partial cache). Pure pres-component.
function ArmCard({ arm }) {
  const a = arm;
  const ready = a.progress.ready_to_propose;
  const bg = ready
    ? "color-mix(in srgb, var(--xmc-success) 8%, transparent)"
    : "color-mix(in srgb, var(--midground) 4%, transparent)";
  return html`
    <div style=${`padding:.4rem .5rem;border-radius:4px;background:${bg};border:1px solid var(--color-border)`}>
      <div style="display:flex;align-items:baseline;gap:.5rem;flex-wrap:wrap;margin-bottom:.3rem">
        <code style="font-size:.78rem;font-weight:600">${a.skill_id}</code>
        <small style="opacity:.7">v${a.version}</small>
        <span class=${`xmc-h-badge xmc-h-badge--${ready ? "success" : "muted"}`}>
          ${ready ? "达阈值，可提议" : "accumulating"}
        </span>
        <small style="margin-left:auto;opacity:.6">
          plays ${a.plays}/10 · mean ${(a.mean_score ?? 0).toFixed(2)}
        </small>
      </div>
      <${ArmProgressBar} progress=${a.progress.plays_progress} label="plays" />
      <${ArmProgressBar} progress=${a.progress.mean_progress} label="mean" />
    </div>
  `;
}


function DreamRow({ row }) {
  const r = row;
  return html`
    <li style="padding:.2rem 0;display:flex;gap:.4rem;flex-wrap:wrap">
      <code>${r.skill_id || "?"}</code>
      ${r.confidence != null
        ? html`<small style="opacity:.6">conf ${(r.confidence).toFixed(2)}</small>`
        : null}
      ${r.title
        ? html`<small style="opacity:.7">${r.title}</small>`
        : null}
      <small style="margin-left:auto;opacity:.5">${fmtIsoLocal(r.ts)}</small>
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
            HonestGrader · EvolutionAgent observer · SkillDreamCycle · ProposalMaterializer 四件套。
            B-167 起新技能提议<strong>自动落到 SKILL.md 并注册</strong>，无需手动 approve；
            evidence 仍随 manifest 写入审计（anti-req #12 借 manifest.evidence 满足）。
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

        <!-- Section 1.5 (B-301): live state of the in-memory chain. -->
        <${LiveStatusPanel} token=${token} />

        <!-- Section 2: Pending proposals -->
        <div>
          <h3 style="margin:0 0 .5rem;font-size:1.05rem">待审进化提议</h3>
          ${proposals.length === 0
            ? html`<div class="xmc-h-empty" style="padding:1rem;font-size:.85rem;line-height:1.6">
                <p style="margin:0 0 .4rem"><strong>暂无近期进化记录。</strong></p>
                <p style="margin:0 0 .3rem;opacity:.8">B-164 + B-167 起进化是<strong>实时 + 自动激活</strong>的：
                每轮对话结束 ~15s 后 SkillProposer 扫最近的 journal，
                找重复出现的 tool 模式，让 LLM 起草新 SKILL.md，
                ProposalMaterializer 收到草稿 → 写到 <code>~/.xmclaw/skills_user/&lt;id&gt;/SKILL.md</code> +
                注册到 SkillRegistry，下一轮即可被 agent 调用。</p>
                <p style="margin:0;opacity:.7;font-size:.78rem">想退回手动审批？
                <code>daemon/config.json</code> 加 <code>"evolution":{"materialize":{"enabled":false}}</code>；
                想关实时触发：<code>"realtime":{"enabled":false}</code>。</p>
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
