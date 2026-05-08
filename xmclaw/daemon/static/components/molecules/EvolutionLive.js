// XMclaw — Evolution-page live status sub-components (B-323 split).
//
// Lifted out of pages/Evolution.js to keep that page under the 500-line
// UI budget (FRONTEND_DESIGN.md §1.4). Four pure presentation
// components plus the LiveStatusPanel that polls
// /api/v2/evolution/snapshot.

const { h } = window.__xmc.preact;
const { useEffect, useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../../lib/api.js";


function fmtIsoLocal(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}


export function ArmProgressBar({ progress, label }) {
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


// B-301 followup: extracted from inline template so the parent's
// .map() doesn't have to embed a multi-line html template literal
// (which seemed to confuse htm's parser when the user's browser
// had partial cache). Pure pres-component.
export function ArmCard({ arm }) {
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


export function DreamRow({ row }) {
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


export function LiveStatusPanel({ token }) {
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
    <div class="xmc-h-skill-card" style="padding:.8rem 1rem">
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
