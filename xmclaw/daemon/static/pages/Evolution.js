// XMclaw — Evolution page
//
// B-16: xm-auto-evo is now the system-level evolution core (not a
// plugin). This page surfaces it FIRST: heartbeat status, signals,
// genes, capsules, recent events. The legacy SkillRegistry feed
// (skill_promoted / candidate / rolled_back) lives below as a
// secondary panel.

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";

const TYPES = "skill_promoted,skill_rolled_back,skill_candidate_proposed";

// ── shared ─────────────────────────────────────────────────────────

// B-36: humanise an epoch timestamp into "just now / 5m ago / 2h ago".
// Cheap fallback — i18n later if we need richer phrasing.
function formatRelative(ts) {
  if (!ts) return "";
  const ms = typeof ts === "number" ? ts * 1000 : Date.parse(ts);
  if (!ms || Number.isNaN(ms)) return "";
  const delta = Math.max(0, Date.now() - ms);
  const sec = Math.floor(delta / 1000);
  if (sec < 30) return "刚刚";
  if (sec < 60) return `${sec}s 前`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m 前`;
  const h = Math.floor(min / 60);
  if (h < 24) return `${h}h 前`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d 前`;
  return new Date(ms).toLocaleDateString("zh-CN");
}

async function postJson(path, token, body = null) {
  const url = path + (token ? `?token=${encodeURIComponent(token)}` : "");
  const init = { method: "POST" };
  if (body !== null) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const r = await fetch(url, init);
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error || d.ok === false) {
    throw new Error(d.error || `HTTP ${r.status}`);
  }
  return d;
}

// ── auto-evo subsystem panel ───────────────────────────────────────

function AutoEvoPanel({ token }) {
  const [status, setStatus] = useState(null);
  const [genes, setGenes] = useState(null);
  const [events, setEvents] = useState(null);
  const [capsules, setCapsules] = useState(null);
  const [learnedSkills, setLearnedSkills] = useState(null);
  const [busy, setBusy] = useState(null); // "start"|"stop"|"observe"|"learn"|"evolve"|null
  const [logTail, setLogTail] = useState(null);

  const loadAll = useCallback(() => {
    apiGet("/api/v2/auto_evo/status", token).then(setStatus).catch(() => setStatus({ wired: false }));
    apiGet("/api/v2/auto_evo/genes", token).then((d) => setGenes(d.genes || [])).catch(() => setGenes([]));
    apiGet("/api/v2/auto_evo/events?tail=50", token).then((d) => setEvents(d.events || [])).catch(() => setEvents([]));
    apiGet("/api/v2/auto_evo/capsules?tail=20", token).then((d) => setCapsules(d.capsules || [])).catch(() => setCapsules([]));
    apiGet("/api/v2/auto_evo/learned_skills?include_disabled=1", token).then((d) => setLearnedSkills(d.skills || [])).catch(() => setLearnedSkills([]));
  }, [token]);

  const loadLog = useCallback(() => {
    apiGet("/api/v2/auto_evo/log?lines=50", token)
      .then((d) => setLogTail(d.lines || []))
      .catch(() => setLogTail(["(log unavailable)"]));
  }, [token]);

  useEffect(() => {
    loadAll();
    const id = setInterval(loadAll, 15_000);
    return () => clearInterval(id);
  }, [loadAll]);

  const onCmd = async (cmd) => {
    setBusy(cmd);
    try {
      if (cmd === "start") await postJson("/api/v2/auto_evo/start", token);
      else if (cmd === "stop") await postJson("/api/v2/auto_evo/stop", token);
      else {
        // run-once command
        const r = await postJson(`/api/v2/auto_evo/run/${cmd}`, token);
        if (r.ok) toast.success(`${cmd}: rc=${r.returncode}`);
        else toast.error(`${cmd}: ${r.error || `rc=${r.returncode}`}`);
      }
      loadAll();
    } catch (e) {
      toast.error(`${cmd}: ${e.message || e}`);
    } finally {
      setBusy(null);
    }
  };

  if (!status) return html`<p class="xmc-datapage__hint">加载进化核心状态…</p>`;
  if (!status.wired) {
    return html`
      <div class="xmc-h-card" style="padding:1rem;line-height:1.65">
        <h3 style="margin:0 0 .5rem">技能进化未启动</h3>
        <p class="xmc-datapage__subtitle" style="margin:0 0 .8rem">
          技能进化（xm-auto-evo）是后台模块：观察你和 agent 的对话，识别重复出现的"动作模式"，
          把它们提炼成可复用的技能（Skill），打分通过后挂载到 agent 的工具集——下次遇到类似场景直接调用，省 token、提速度。
        </p>
        <p class="xmc-datapage__subtitle" style="margin:0 0 .4rem;font-size:.82rem">
          没启动有两种可能：
        </p>
        <ul class="xmc-datapage__subtitle" style="margin:0 0 .6rem 1.2rem;font-size:.82rem">
          <li>config 里 <code>evolution.enabled</code> 是 false（默认 true，可改回）</li>
          <li>daemon 启动时初始化失败 — <a href="/ui/logs">查看 logs</a> 找具体报错</li>
        </ul>
        <p class="xmc-datapage__subtitle" style="margin:0;font-size:.78rem;color:var(--xmc-fg-muted)">
          新手可忽略 — 这功能要在你已经跟 agent 反复打交道一段时间之后才会显出价值。
        </p>
      </div>
    `;
  }

  return html`
    <div>
      <div class="xmc-h-card" style="padding:1rem;margin-bottom:1rem">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:.5rem;flex-wrap:wrap">
          <h3 style="margin:0">🧬 进化核心 (xm-auto-evo)</h3>
          <small class="xmc-datapage__subtitle">
            ${status.running
              ? html`<${Badge} tone="success">运行中</${Badge}> · pid=${status.pid}`
              : html`<${Badge} tone="warn">已停止</${Badge}>`}
          </small>
        </div>
        <p class="xmc-datapage__subtitle" style="margin:.4rem 0 .8rem">
          这是 XMclaw 的自主进化心脏 —
          <strong>系统级</strong>子系统，daemon 启动时自动拉起。
          它观察对话信号、检测重复模式、自动生成 Gene/Skill。
          工作目录 <code>${status.workspace}</code>。
        </p>
        <div style="display:flex;gap:.75rem;margin:.5rem 0;flex-wrap:wrap">
          <div class="xmc-datapage__row" style="flex:1;min-width:120px;background:color-mix(in srgb, var(--color-primary, #6aa3f0) 14%, transparent);border-color:color-mix(in srgb, var(--color-primary, #6aa3f0) 35%, transparent)">
            <small>已学技能 (agent 可用)</small>
            <strong style="font-size:1.6rem">${status.counts?.learned_skills ?? (learnedSkills || []).length}</strong>
          </div>
          <div class="xmc-datapage__row" style="flex:1;min-width:120px">
            <small>事件</small>
            <strong style="font-size:1.4rem">${status.counts?.events || 0}</strong>
          </div>
          <div class="xmc-datapage__row" style="flex:1;min-width:120px">
            <small>基因 (Gene)</small>
            <strong style="font-size:1.4rem">${status.counts?.genes || 0}</strong>
          </div>
          <div class="xmc-datapage__row" style="flex:1;min-width:120px">
            <small>封包 (Capsule)</small>
            <strong style="font-size:1.4rem">${status.counts?.capsules || 0}</strong>
          </div>
        </div>
        <div style="display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.5rem">
          ${status.running
            ? html`<button class="xmc-h-btn" onClick=${() => onCmd("stop")} disabled=${busy != null}>${busy === "stop" ? "停止中…" : "停止"}</button>`
            : html`<button class="xmc-h-btn xmc-h-btn--primary" onClick=${() => onCmd("start")} disabled=${busy != null}>${busy === "start" ? "启动中…" : "启动"}</button>`}
          <button class="xmc-h-btn" onClick=${() => onCmd("observe")} disabled=${busy != null}>${busy === "observe" ? "运行中…" : "立即观察"}</button>
          <button class="xmc-h-btn" onClick=${() => onCmd("learn")} disabled=${busy != null}>${busy === "learn" ? "运行中…" : "立即学习"}</button>
          <button class="xmc-h-btn" onClick=${() => onCmd("evolve")} disabled=${busy != null}>${busy === "evolve" ? "运行中…" : "立即进化"}</button>
          <button class="xmc-h-btn xmc-h-btn--ghost" onClick=${loadLog}>查看日志</button>
        </div>
        ${logTail
          ? html`
              <pre style="margin:.6rem 0 0;padding:.5rem;background:var(--color-bg);border-radius:4px;max-height:14rem;overflow:auto;font-family:var(--xmc-font-mono);font-size:.7rem;line-height:1.4;white-space:pre-wrap">${logTail.slice(-50).join("\n")}</pre>
            `
          : null}
      </div>

      <h3 style="margin:1rem 0 .5rem">已学技能（agent 实际可调用）${(learnedSkills || []).length ? ` (${learnedSkills.length})` : ""}</h3>
      <p class="xmc-datapage__subtitle" style="margin-bottom:.5rem">
        这些 SKILL.md 已被 LearnedSkillsLoader 注入到 system prompt — agent 在下一轮对话能直接读到、按 trigger 触发。
        Gene/Capsule 是调度器内部状态，<strong>真正能用的是这里列出的 Skill</strong>。
      </p>
      ${(learnedSkills || []).length === 0
        ? html`<p class="xmc-datapage__empty">还没有 — 当 xm-auto-evo 检测到重复模式后会自动生成</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${(learnedSkills || []).slice(0, 20).map((s) => {
                const triggers = (s.triggers || []).slice(0, 4);
                const invokes = s.invocation_count || 0;
                const invokes30d = s.invocation_count_30d || 0;
                const isStale = !!s.is_stale;
                const outcomes = s.outcomes || { success: 0, partial: 0, error: 0 };
                const totalOut = (outcomes.success || 0) + (outcomes.partial || 0) + (outcomes.error || 0);
                const successRate = totalOut > 0
                  ? Math.round((outcomes.success || 0) * 100 / totalOut)
                  : null;
                const isDisabled = !!s.disabled;
                const onToggle = async () => {
                  try {
                    await postJson(
                      `/api/v2/auto_evo/learned_skills/${encodeURIComponent(s.skill_id)}/disable`,
                      token,
                      { disabled: !isDisabled },
                    );
                    toast.success(isDisabled ? `${s.skill_id} 已恢复` : `${s.skill_id} 已暂停`);
                    loadAll();
                  } catch (e) {
                    toast.error(`切换失败: ${e.message || e}`);
                  }
                };
                return html`
                  <li class="xmc-datapage__row" key=${s.skill_id}
                      style=${isDisabled ? "opacity:.55" : ""}>
                    <div style="display:flex;justify-content:space-between;align-items:baseline;gap:.5rem;flex-wrap:wrap">
                      <strong>${s.title || s.skill_id}</strong>
                      <span style="display:flex;gap:.4rem;align-items:center">
                        ${isDisabled
                          ? (s.auto_disabled_ts
                              ? html`<span
                                  class="xmc-h-badge xmc-h-badge--error"
                                  title=${`B-36 自动暂停: ${s.auto_disabled_streak || 0} 次连续报错 @ ${formatRelative(s.auto_disabled_ts)}`}
                                >🚫 auto-停 (${s.auto_disabled_streak || "?"} err)</span>`
                              : html`<span class="xmc-h-badge xmc-h-badge--warn" title="已暂停 - agent 看不到">⏸ 暂停</span>`)
                          : null}
                        ${isStale && !isDisabled
                          ? html`<span class="xmc-h-badge xmc-h-badge--warn" title="60+ 天未触发且最近 30 天 0 调用 — 考虑暂停">🕸 stale</span>`
                          : null}
                        ${invokes > 0
                          ? html`<span class="xmc-h-badge xmc-h-badge--success" title=${`总计调用 ${invokes} 次，最近 30 天 ${invokes30d} 次（B-120）`}>⚡ ${invokes}${invokes30d > 0 ? ` · ${invokes30d}/30d` : ""}</span>`
                          : html`<span class="xmc-h-badge xmc-h-badge--muted" title="尚未观察到调用">0 用</span>`}
                        ${successRate !== null
                          ? html`<span
                              class=${"xmc-h-badge xmc-h-badge--" + (successRate >= 80 ? "success" : successRate >= 50 ? "warn" : "error")}
                              title=${`${outcomes.success || 0} 成功 / ${outcomes.partial || 0} 部分 / ${outcomes.error || 0} 失败 (B-35)`}
                            >${successRate}% ✓</span>`
                          : null}
                        <code style="font-size:.7rem;color:var(--xmc-fg-muted)">${s.skill_id}</code>
                        <button
                          class="xmc-h-btn xmc-h-btn--ghost"
                          style="padding:.15rem .5rem;font-size:.7rem"
                          title=${isDisabled ? "恢复 - agent 下一轮重新看到" : "暂停 - 写 disabled:true 到 SKILL.md frontmatter"}
                          onClick=${onToggle}
                        >${isDisabled ? "恢复" : "暂停"}</button>
                      </span>
                    </div>
                    ${s.description
                      ? html`<small style="display:block;margin-top:.2rem;color:var(--xmc-fg-muted)">${s.description.slice(0, 160)}</small>`
                      : null}
                    ${s.last_fired_ts
                      ? html`<small style="display:block;margin-top:.15rem;color:var(--xmc-fg-muted);font-size:.7rem">最近触发: ${formatRelative(s.last_fired_ts)}</small>`
                      : null}
                    ${triggers.length
                      ? html`
                          <div style="margin-top:.3rem;display:flex;gap:.3rem;flex-wrap:wrap">
                            ${triggers.map((t) => html`<code style="font-size:.7rem;background:color-mix(in srgb, var(--color-primary, #6aa3f0) 12%, transparent);padding:1px 6px;border-radius:4px" key=${t}>${t}</code>`)}
                          </div>
                        `
                      : html`<small style="margin-top:.2rem;display:block;color:var(--xmc-fg-muted);font-size:.7rem">⚠ 这个 SKILL 没有 trigger，agent 可能不知道何时调它（B-23 已修写入器）</small>`}
                  </li>
                `;
              })}
            </ul>
          `}

      <h3 style="margin:1rem 0 .5rem">已注册 Gene (${(genes || []).length})</h3>
      ${(genes || []).length === 0
        ? html`<p class="xmc-datapage__empty">还没有自动生成的 Gene — 等系统观察到重复模式后会自动创建</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${(genes || []).slice(0, 10).map((g) => html`
                <li class="xmc-datapage__row" key=${g.id}>
                  <div style="display:flex;justify-content:space-between;gap:.5rem">
                    <strong>${g.id}</strong>
                    <${Badge} tone="info">${g.category || "?"}</${Badge}>
                  </div>
                  <small style="display:block;color:var(--xmc-fg-muted);margin-top:.2rem">
                    匹配信号: ${(g.signals_match || []).slice(0, 3).join(", ") || "(none)"}
                    · v_score=${g.v_score ?? "?"}
                  </small>
                </li>
              `)}
            </ul>
          `}

      <h3 style="margin:1rem 0 .5rem">最近事件 (${(events || []).length})</h3>
      ${(events || []).length === 0
        ? html`<p class="xmc-datapage__empty">还没有事件 — 进化心跳每 30 分钟运行一次</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${(events || []).slice(-15).reverse().map((e, i) => {
                const ts = e.timestamp || e.ts;
                const tsStr = ts ? new Date(typeof ts === "number" ? ts * 1000 : ts).toLocaleString() : "";
                return html`
                  <li class="xmc-datapage__row" key=${i}>
                    <div style="display:flex;justify-content:space-between;gap:.5rem">
                      <${Badge} tone="muted">${e.event_type || e.type || "?"}</${Badge}>
                      <small>${tsStr}</small>
                    </div>
                    <code style="display:block;margin-top:.2rem;font-size:.7rem">${JSON.stringify(e.payload || {}).slice(0, 140)}</code>
                  </li>
                `;
              })}
            </ul>
          `}
    </div>
  `;
}

// ── legacy skill events panel ──────────────────────────────────────

function SkillEventsPanel({ token }) {
  const [events, setEvents] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiGet(`/api/v2/events?limit=200&types=${TYPES}`, token)
      .then((d) => { if (!cancelled) setEvents(d.events || []); })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [token]);

  if (error) return html`<p class="xmc-datapage__error">${error}</p>`;
  if (!events) return html`<p class="xmc-datapage__hint">加载技能事件…</p>`;

  const now = Date.now() / 1000;
  const dayAgo = now - 86400;
  const today = events.filter((e) => e.ts >= dayAgo);
  const promoted = today.filter((e) => e.type === "skill_promoted").length;
  const rolledBack = today.filter((e) => e.type === "skill_rolled_back").length;
  const candidates = today.filter((e) => e.type === "skill_candidate_proposed").length;

  return html`
    <div>
      <div style="display:flex;gap:.75rem;margin-bottom:.6rem;flex-wrap:wrap">
        <div class="xmc-datapage__row" style="flex:1;min-width:100px">
          <small>今日晋升</small>
          <strong style="font-size:1.2rem">${promoted}</strong>
        </div>
        <div class="xmc-datapage__row" style="flex:1;min-width:100px">
          <small>今日回滚</small>
          <strong style="font-size:1.2rem">${rolledBack}</strong>
        </div>
        <div class="xmc-datapage__row" style="flex:1;min-width:100px">
          <small>今日候选</small>
          <strong style="font-size:1.2rem">${candidates}</strong>
        </div>
      </div>
      ${events.length === 0
        ? html`<p class="xmc-datapage__empty">SkillRegistry 还没有事件</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${events.slice(-10).reverse().map((e) => {
                const ts = e.ts ? new Date(e.ts * 1000).toLocaleString() : "";
                return html`
                  <li class="xmc-datapage__row" key=${e.id || ts}>
                    <div style="display:flex;justify-content:space-between;gap:.5rem">
                      <${Badge} tone="muted">${e.type}</${Badge}>
                      <small>${ts}</small>
                    </div>
                    <code>${JSON.stringify(e.payload).slice(0, 120)}</code>
                  </li>
                `;
              })}
            </ul>
          `}
    </div>
  `;
}

// ── shell ──────────────────────────────────────────────────────────

export function EvolutionPage({ token }) {
  return html`
    <section class="xmc-datapage" aria-labelledby="evo-title">
      <header class="xmc-datapage__header">
        <h2 id="evo-title">进化 ★</h2>
        <p class="xmc-datapage__subtitle">
          XMclaw 的自主进化系统。上方是 <strong>xm-auto-evo</strong>（系统级进化心脏 —
          自动观察、模式识别、Gene/Skill 自动生成）；下方是 SkillRegistry 的晋升/回滚事件。
        </p>
      </header>
      <${AutoEvoPanel} token=${token} />
      <h3 style="margin:1.5rem 0 .5rem">技能注册中心事件</h3>
      <${SkillEventsPanel} token=${token} />
    </section>
  `;
}
