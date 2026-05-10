// XMclaw — Mind ▸ "建议盒子" panel (R6, 2026-05-10).
//
// Renders the SuggestionInbox: pending proactive suggestions the
// AutonomyPolicy decided to surface (rather than silently apply or
// drop). Operator approves / rejects each one; the daemon routes
// approvals into the existing Evolution / Persona / TaskScheduler
// pipelines.
//
// Data: GET /api/v2/cognition/suggestions?status=...
// Mutate: POST /api/v2/cognition/suggestions/{id}/{approve,reject}

const { h } = window.__xmc.preact;
const { useState, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { useSafeFetch, useSafePost } from "../../lib/use_safe_fetch.js";
import { toast } from "../../lib/toast.js";

const RISK_COLORS = {
  low:    "#2ecc71",
  medium: "#f39c12",
  high:   "#e74c3c",
};

const KIND_LABELS = {
  curriculum_edit:    "课程修改",
  preference_update:  "偏好更新",
  skill_propose:      "新技能提案",
  memory_consolidate: "记忆整理",
  goal_add:           "新增目标",
  task_submit:        "新建任务",
  send_notification:  "发通知",
  open_url:           "打开 URL",
};

function fmtTime(ts) {
  if (!ts) return "—";
  try {
    const d = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getMonth()+1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch (_) { return String(ts); }
}

function SuggestionCard({ sg, onApprove, onReject, busy }) {
  const riskColor = RISK_COLORS[sg.risk] || "#888";
  const kindLabel = KIND_LABELS[sg.kind] || sg.kind;
  const isPending = sg.status === "pending";
  return html`
    <div style="border:1px solid var(--color-border);border-radius:8px;padding:.8rem 1rem;margin-bottom:.7rem;background:var(--color-surface)">
      <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem;align-items:center">
        <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap">
          <span style=${`font-size:.7rem;padding:1px 6px;border-radius:3px;background:${riskColor}22;color:${riskColor};border:1px solid ${riskColor}55`}>
            ${sg.risk} risk
          </span>
          <strong>${kindLabel}</strong>
          <span style="font-size:.72rem;opacity:.6">conf ${sg.confidence?.toFixed?.(2) ?? "?"}</span>
        </div>
        <span style="font-size:.7rem;opacity:.6">${fmtTime(sg.ts)} · ${sg.source}</span>
      </div>
      <div style="margin-top:.4rem;font-size:.9rem;word-break:break-word">
        ${sg.summary}
      </div>
      ${sg.payload && Object.keys(sg.payload).length > 0 ? html`
        <details style="margin-top:.4rem">
          <summary style="cursor:pointer;font-size:.74rem;opacity:.7">展开详情</summary>
          <pre style="margin:.3rem 0 0;font-family:var(--xmc-font-mono, monospace);font-size:.72rem;white-space:pre-wrap;word-break:break-word;background:var(--color-background);padding:.4rem;border-radius:4px">${JSON.stringify(sg.payload, null, 2)}</pre>
        </details>
      ` : null}
      <div style="margin-top:.6rem;display:flex;gap:.4rem;align-items:center">
        ${isPending ? html`
          <button type="button" class="xmc-h-btn" disabled=${busy} onClick=${() => onApprove(sg.id)} style="background:${RISK_COLORS.low}22;color:${RISK_COLORS.low};border:1px solid ${RISK_COLORS.low}55">✓ 批准</button>
          <button type="button" class="xmc-h-btn" disabled=${busy} onClick=${() => onReject(sg.id)} style="background:#88888822">✗ 拒绝</button>
        ` : html`
          <span style="font-size:.78rem;opacity:.7">
            ${sg.status === "approved" ? "✓ 已批准" :
              sg.status === "rejected" ? "✗ 已拒绝" :
              sg.status === "expired"  ? "⏱ 已过期" :
              sg.status === "applied"  ? "✓ 已执行" : sg.status}
            ${sg.decided_by ? ` (${sg.decided_by})` : ""}
          </span>
        `}
        ${sg.verdict === "needs_confirmation" && isPending ? html`
          <span style="font-size:.7rem;opacity:.65;margin-left:.5rem">
            ⚠ 高风险 — 必须用户确认
          </span>
        ` : null}
      </div>
    </div>
  `;
}

export function SuggestionsPanel({ token }) {
  const [statusFilter, setStatusFilter] = useState("pending");
  const [data, setData] = useState({ suggestions: [], pending_total: 0 });
  const url = `/api/v2/cognition/suggestions?status=${statusFilter}&limit=100`;
  const { loading, error, refresh } = useSafeFetch(url, token, setData, [statusFilter]);
  const { run: postFn, loading: posting } = useSafePost(token);

  const onApprove = useCallback(async (sgId) => {
    const r = await postFn("POST", `/api/v2/cognition/suggestions/${sgId}/approve`, {});
    if (r.ok) {
      toast.success("已批准");
      refresh();
    } else {
      toast.error(`批准失败: ${r.error?.message || r.error}`);
    }
  }, [postFn, refresh]);

  const onReject = useCallback(async (sgId) => {
    const r = await postFn("POST", `/api/v2/cognition/suggestions/${sgId}/reject`, {});
    if (r.ok) {
      toast.success("已拒绝");
      refresh();
    } else {
      toast.error(`拒绝失败: ${r.error?.message || r.error}`);
    }
  }, [postFn, refresh]);

  if (loading && (!data.suggestions || data.suggestions.length === 0)) {
    return html`<div class="xmc-h-loading" style="padding:2rem;text-align:center">加载建议…</div>`;
  }
  if (error) {
    // 503 means SuggestionInbox isn't wired yet (continuous_loop disabled).
    const status = error.status;
    const isNotWired = status === 503;
    return html`
      <div class="xmc-h-error" role="alert" style="padding:1rem">
        <strong>${isNotWired ? "建议盒子未启用" : "加载失败"}</strong>
        <div style="font-size:.8rem;opacity:.85;margin-top:.4rem">
          ${isNotWired ? html`
            把以下加到 <code>daemon/config.json</code> 然后重启 daemon:
            <pre style="margin:.4rem 0 0;font-family:var(--xmc-font-mono, monospace);font-size:.75rem;background:var(--color-background);padding:.4rem;border-radius:4px">${`"cognition": {
  "continuous_loop": {
    "enabled": true,
    "autonomy_level": 50
  }
}`}</pre>
          ` : String(error.message || error)}
        </div>
        <button type="button" class="xmc-h-btn" style="margin-top:.5rem" onClick=${() => refresh()}>重试</button>
      </div>
    `;
  }

  const sgs = data.suggestions || [];

  return html`
    <section>
      <div style="background:var(--color-surface);border:1px solid var(--color-border);border-radius:8px;padding:.75rem 1rem;margin-bottom:1rem">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem">
          <div>
            <strong>📥 主动建议</strong>
            <div style="font-size:.78rem;opacity:.7;margin-top:.2rem">
              待审 ${data.pending_total ?? 0} 条 · 当前显示 ${sgs.length} 条
            </div>
          </div>
          <div style="display:flex;gap:.4rem;flex-wrap:wrap">
            ${["pending", "approved", "rejected", "applied", "all"].map((s) => html`
              <button key=${s} type="button" onClick=${() => setStatusFilter(s)}
                style=${`appearance:none;border:1px solid var(--color-border);background:${statusFilter === s ? "var(--color-primary)" : "transparent"};color:${statusFilter === s ? "var(--color-bg)" : "inherit"};padding:.2rem .6rem;border-radius:4px;font-size:.74rem;cursor:pointer`}>
                ${s}
              </button>
            `)}
            <button type="button" class="xmc-h-btn" onClick=${() => refresh()}>刷新</button>
          </div>
        </div>
        <div style="font-size:.72rem;opacity:.6;margin-top:.5rem;line-height:1.4">
          这些是 R3 元认知 + R4 多模态感知给出的「主动建议」。
          AutonomyPolicy 决定要 surface 还是 auto-apply；这里看到的都是
          需要你过目的 (低置信 / 高风险 / 中风险离开期间)。
        </div>
      </div>
      ${sgs.length === 0 ? html`
        <div style="opacity:.6;font-size:.9rem;padding:2rem;text-align:center;border:1px dashed var(--color-border);border-radius:8px">
          ${statusFilter === "pending" ? "暂无待审建议 — agent 还没找到值得提的事" : `没有 ${statusFilter} 状态的建议`}
        </div>
      ` : html`
        <div>
          ${sgs.map((sg) => html`
            <${SuggestionCard} key=${sg.id} sg=${sg} onApprove=${onApprove} onReject=${onReject} busy=${posting} />
          `)}
        </div>
      `}
    </section>
  `;
}
