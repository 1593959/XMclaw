// XMclaw — CronPage 1:1 port of hermes-agent CronPage.tsx
//
// Layout (CronPage.tsx:151-353):
//   1. Top "New job" Card with Name / Prompt / Schedule / Deliver +
//      Create button (port of CronPage.tsx:170-249)
//   2. Job list header — "Scheduled Jobs (N)"
//   3. Job rows — Card per job, info column (name + state/deliver
//      badges + schedule + last/next + last_error) + action column
//      (pause-resume + trigger + delete) — port of :269-349
//
// Data: backed by /api/v2/cron (POST/DELETE/GET) wired to CronStore.
// XMclaw schema differences vs Hermes:
//   - state="paused" → we use the `enabled: bool` flag; the visual
//     state badge maps enabled→success, disabled→warning, error→destructive
//   - deliver — Hermes routes by channel (local/telegram/discord/slack/
//     email); our jobs carry `agent_id` instead. UI mirrors the dropdown
//     anyway so the visual surface is 1:1.
//   - trigger-now — not yet wired; button kept for visual parity, calls
//     a stub that toasts "未实现"

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../lib/api.js";
import { toast } from "../lib/toast.js";

function Icon({ d, className }) {
  return html`
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
         class=${"xmc-icon " + (className || "")} aria-hidden="true">
      <path d=${d} />
    </svg>
  `;
}

const I_PLUS  = "M12 5v14 M5 12h14";
const I_CLOCK = "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2";
const I_PAUSE = "M14 4h4v16h-4z M6 4h4v16H6z";
const I_PLAY  = "M5 4 19 12 5 20Z";
const I_ZAP   = "M13 2 3 14h9l-1 8 10-12h-9l1-8z";
const I_TRASH = "M3 6h18 M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6 M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2";

function formatTime(epoch) {
  if (!epoch) return "—";
  const d = new Date(epoch * 1000);
  return d.toLocaleString();
}

function jobState(job) {
  if (!job.enabled) return { label: "paused",  tone: "warning" };
  if (job.last_error)   return { label: "error",   tone: "destructive" };
  return { label: "scheduled", tone: "success" };
}

// ── New-job form (Card) ──────────────────────────────────────────

function NewJobForm({ onCreated, token }) {
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [schedule, setSchedule] = useState("every 1h");
  const [deliver, setDeliver] = useState("local");
  const [creating, setCreating] = useState(false);

  const onCreate = async () => {
    if (!prompt.trim() || !schedule.trim()) {
      toast.error("Prompt 和 Schedule 必填");
      return;
    }
    setCreating(true);
    try {
      const res = await fetch(
        "/api/v2/cron" + (token ? `?token=${encodeURIComponent(token)}` : ""),
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            name: name.trim() || prompt.trim().slice(0, 32),
            prompt: prompt.trim(),
            schedule: schedule.trim(),
            agent_id: deliver === "local" ? "main" : deliver,
          }),
        }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      toast.success("Cron 任务已创建 ✓");
      setName(""); setPrompt(""); setSchedule("every 1h"); setDeliver("local");
      onCreated && onCreated();
    } catch (e) {
      toast.error("创建失败：" + (e.message || e));
    } finally {
      setCreating(false);
    }
  };

  return html`
    <div class="xmc-h-card">
      <h3 class="xmc-h-card__title xmc-h-cron__title">
        <${Icon} d=${I_PLUS} className="xmc-h-icon" />
        新建任务
      </h3>
      <div class="xmc-h-cron__form">
        <label class="xmc-h-cron__field">
          <span class="xmc-h-cron__label">名称（可选）</span>
          <input
            class="xmc-h-input"
            placeholder="每天回顾"
            value=${name}
            onInput=${(e) => setName(e.target.value)}
          />
        </label>

        <label class="xmc-h-cron__field">
          <span class="xmc-h-cron__label">提示词 *</span>
          <textarea
            class="xmc-h-input xmc-h-cron__textarea"
            placeholder="给 agent 发的提示词，例如：检查 GitHub 通知…"
            value=${prompt}
            onInput=${(e) => setPrompt(e.target.value)}
            rows="3"
          ></textarea>
        </label>

        <div class="xmc-h-cron__row3">
          <label class="xmc-h-cron__field">
            <span class="xmc-h-cron__label">调度 *</span>
            <input
              class="xmc-h-input"
              placeholder="every 5m / 0 9 * * *"
              value=${schedule}
              onInput=${(e) => setSchedule(e.target.value)}
            />
          </label>

          <label class="xmc-h-cron__field">
            <span class="xmc-h-cron__label">投递目标</span>
            <select
              class="xmc-h-input"
              value=${deliver}
              onChange=${(e) => setDeliver(e.target.value)}
            >
              <option value="local">本地（main agent）</option>
              <option value="telegram">Telegram</option>
              <option value="discord">Discord</option>
              <option value="feishu">飞书</option>
              <option value="dingtalk">钉钉</option>
              <option value="wecom">企业微信</option>
              <option value="email">邮件</option>
            </select>
          </label>

          <div class="xmc-h-cron__field xmc-h-cron__field--align-end">
            <button
              type="button"
              class="xmc-h-btn xmc-h-btn--primary xmc-h-cron__create"
              onClick=${onCreate}
              disabled=${creating}
            >
              <${Icon} d=${I_PLUS} className="xmc-h-icon" />
              ${creating ? "创建中…" : "创建"}
            </button>
          </div>
        </div>
      </div>
    </div>
  `;
}

// ── Job row ──────────────────────────────────────────────────────

function JobRow({ job, onDelete, onToggle, onTrigger, busy }) {
  const state = jobState(job);
  const title = job.name || (job.prompt || "").slice(0, 60);
  return html`
    <div class="xmc-h-card xmc-h-cron__job">
      <div class="xmc-h-cron__job-info">
        <div class="xmc-h-cron__job-head">
          <span class="xmc-h-cron__job-title">${title}</span>
          <span class=${"xmc-h-badge xmc-h-badge--" + state.tone}>${state.label}</span>
          ${job.agent_id && job.agent_id !== "main"
            ? html`<span class="xmc-h-badge">${job.agent_id}</span>`
            : null}
        </div>
        ${job.name
          ? html`<p class="xmc-h-cron__job-prompt">${(job.prompt || "").slice(0, 120)}${(job.prompt || "").length > 120 ? "…" : ""}</p>`
          : null}
        <div class="xmc-h-cron__job-meta">
          <code class="xmc-h-cron__sched">${job.schedule}</code>
          <span>last: ${formatTime(job.last_run_at)}</span>
          <span>next: ${formatTime(job.next_run_at)}</span>
          <span>runs: ${job.run_count || 0}</span>
        </div>
        ${job.last_error
          ? html`<p class="xmc-h-cron__job-err">${job.last_error}</p>`
          : null}
      </div>

      <div class="xmc-h-cron__job-actions">
        <button
          type="button"
          class="xmc-h-btn xmc-h-btn--ghost"
          onClick=${onToggle}
          disabled=${busy}
          title=${job.enabled ? "暂停" : "恢复"}
          aria-label=${job.enabled ? "pause" : "resume"}
        >
          <${Icon} d=${job.enabled ? I_PAUSE : I_PLAY} />
        </button>
        <button
          type="button"
          class="xmc-h-btn xmc-h-btn--ghost"
          onClick=${onTrigger}
          disabled=${busy}
          title="立即触发（未实现）"
          aria-label="trigger"
        >
          <${Icon} d=${I_ZAP} />
        </button>
        <button
          type="button"
          class="xmc-h-btn xmc-h-btn--ghost"
          onClick=${onDelete}
          disabled=${busy}
          title="删除"
          aria-label="delete"
        >
          <${Icon} d=${I_TRASH} className="xmc-h-cron__trash" />
        </button>
      </div>
    </div>
  `;
}

// ── Page ─────────────────────────────────────────────────────────

export function CronPage({ token }) {
  const [jobs, setJobs] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);

  const load = useCallback(() => {
    apiGet("/api/v2/cron", token)
      .then((d) => setJobs(d.jobs || []))
      .catch((e) => setError(String(e.message || e)));
  }, [token]);

  useEffect(() => { load(); }, [load]);

  const onDelete = async (jobId) => {
    if (!confirm("确认删除这个 cron 任务？")) return;
    setBusy(jobId);
    try {
      const res = await fetch(
        `/api/v2/cron/${encodeURIComponent(jobId)}` +
          (token ? `?token=${encodeURIComponent(token)}` : ""),
        { method: "DELETE" }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      toast.success("已删除");
      load();
    } catch (e) {
      toast.error("删除失败：" + (e.message || e));
    } finally {
      setBusy(null);
    }
  };

  const _post = async (path) => {
    const res = await fetch(
      "/api/v2/cron" + path +
        (token ? `?token=${encodeURIComponent(token)}` : ""),
      { method: "POST" }
    );
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  };

  const onTrigger = async (jobId) => {
    setBusy(jobId);
    try {
      await _post(`/${encodeURIComponent(jobId)}/trigger`);
      toast.success("已触发，将在下个 tick 运行");
      load();
    } catch (e) {
      toast.error("触发失败：" + (e.message || e));
    } finally {
      setBusy(null);
    }
  };

  const onToggle = async (jobId) => {
    const job = (jobs || []).find((j) => j.id === jobId);
    if (!job) return;
    const action = job.enabled ? "pause" : "resume";
    setBusy(jobId);
    try {
      const data = await _post(`/${encodeURIComponent(jobId)}/${action}`);
      toast.success(action === "pause" ? "已暂停" : "已恢复");
      load();
    } catch (e) {
      toast.error((action === "pause" ? "暂停" : "恢复") + "失败：" + (e.message || e));
    } finally {
      setBusy(null);
    }
  };

  if (error) {
    return html`
      <section class="xmc-h-page" aria-labelledby="cron-title">
        <header class="xmc-h-page__header">
          <h2 id="cron-title" class="xmc-h-page__title">Cron</h2>
        </header>
        <div class="xmc-h-page__body">
          <div class="xmc-h-error">${error}</div>
        </div>
      </section>
    `;
  }

  return html`
    <section class="xmc-h-page" aria-labelledby="cron-title">
      <header class="xmc-h-page__header">
        <div class="xmc-h-page__heading">
          <h2 id="cron-title" class="xmc-h-page__title">Cron 调度</h2>
          <p class="xmc-h-page__subtitle">
            周期性执行的 agent 任务。支持 <code>every Nu</code>（5s/m/h/d）+
            完整 cron 语法（需 <code>croniter</code>）。jobs.json 落在
            <code>~/.xmclaw/cron/</code>。
          </p>
        </div>
        <div class="xmc-h-page__actions">
          <span class="xmc-h-badge">${(jobs || []).length} 个</span>
        </div>
      </header>

      <div class="xmc-h-page__body xmc-h-cron__body">
        <${NewJobForm} onCreated=${load} token=${token} />

        <h3 class="xmc-h-cron__listhead">
          <${Icon} d=${I_CLOCK} className="xmc-h-icon" />
          已调度任务 (${(jobs || []).length})
        </h3>

        ${jobs === null
          ? html`<div class="xmc-h-loading">载入中…</div>`
          : jobs.length === 0
            ? html`<div class="xmc-h-empty">还没有任务 — 用上方表单创建第一个。</div>`
            : html`
              <div class="xmc-h-cron__list">
                ${jobs.map((j) => html`
                  <${JobRow}
                    key=${j.id}
                    job=${j}
                    busy=${busy === j.id}
                    onDelete=${() => onDelete(j.id)}
                    onTrigger=${() => onTrigger(j.id)}
                    onToggle=${() => onToggle(j.id)}
                  />
                `)}
              </div>
            `}
      </div>
    </section>
  `;
}
