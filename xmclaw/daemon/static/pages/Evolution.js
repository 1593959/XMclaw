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

async function postJson(path, token) {
  const url = path + (token ? `?token=${encodeURIComponent(token)}` : "");
  const r = await fetch(url, { method: "POST" });
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
  const [busy, setBusy] = useState(null); // "start"|"stop"|"observe"|"learn"|"evolve"|null
  const [logTail, setLogTail] = useState(null);

  const loadAll = useCallback(() => {
    apiGet("/api/v2/auto_evo/status", token).then(setStatus).catch(() => setStatus({ wired: false }));
    apiGet("/api/v2/auto_evo/genes", token).then((d) => setGenes(d.genes || [])).catch(() => setGenes([]));
    apiGet("/api/v2/auto_evo/events?tail=50", token).then((d) => setEvents(d.events || [])).catch(() => setEvents([]));
    apiGet("/api/v2/auto_evo/capsules?tail=20", token).then((d) => setCapsules(d.capsules || [])).catch(() => setCapsules([]));
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
      <div class="xmc-h-card" style="padding:1rem">
        <h3 style="margin:0 0 .5rem">⚠️ xm-auto-evo 未挂载</h3>
        <p class="xmc-datapage__subtitle">
          配置中 <code>evolution.auto_evo.enabled</code> 为 false，或 daemon 启动时
          初始化失败。详见日志。
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
          <div class="xmc-datapage__row" style="flex:1;min-width:120px">
            <small>已注册 Gene</small>
            <strong style="font-size:1.4rem">${(genes || []).length}</strong>
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
