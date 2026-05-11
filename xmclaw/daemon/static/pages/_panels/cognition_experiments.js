// XMclaw — Experiments panel for CognitionPage (Iteration 3)
//
// Surfaces A/B experiment results from SelfExperimentLoop.

const { h } = window.__xmc.preact;
const { useEffect, useState } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet } from "../../lib/api.js";

function _fmtTs(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function _toneForDecision(d) {
  if (d === "adopt") return "success";
  if (d === "reject") return "error";
  if (d === "extend") return "warn";
  return "muted";
}

export function ExperimentsPanel({ token }) {
  const [rows, setRows] = useState([]);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    try {
      const data = await apiGet("/api/v2/cognition/experiments?limit=30", token);
      setRows(data.experiments || []);
    } catch (e) {
      toast.error("加载实验失败: " + String(e.message || e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [token]);

  if (loading) return html`<div style="padding:2rem;text-align:center">加载实验…</div>`;

  if (rows.length === 0) {
    return html`
      <div style="padding:2rem;text-align:center;color:var(--xmc-fg-muted)">
        <div style="font-size:1.1rem;margin-bottom:.5rem">🔬 还没有实验</div>
        <div style="font-size:.85rem">daemon 会在后台自动运行 A/B 测试，结果会显示在这里。</div>
      </div>
    `;
  }

  return html`
    <div>
      <table style="width:100%;border-collapse:collapse;font-size:.85rem">
        <thead>
          <tr style="text-align:left;border-bottom:1px solid var(--color-border)">
            <th style="padding:.4rem .6rem">时间</th>
            <th style="padding:.4rem .6rem">假设</th>
            <th style="padding:.4rem .6rem">Metric</th>
            <th style="padding:.4rem .6rem">Decision</th>
            <th style="padding:.4rem .6rem">Δ</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((r) => html`
            <tr key=${r.id} style="border-bottom:1px solid var(--color-border);cursor:pointer"
                onClick=${() => setDetail(r)}>
              <td style="padding:.4rem .6rem;white-space:nowrap">${_fmtTs(r.started_at)}</td>
              <td style="padding:.4rem .6rem">${r.hypothesis}</td>
              <td style="padding:.4rem .6rem">${r.metric}</td>
              <td style="padding:.4rem .6rem">
                <span class=${"xmc-badge xmc-badge--" + _toneForDecision(r.result?.decision)}>
                  ${r.result?.decision || "running"}
                </span>
              </td>
              <td style="padding:.4rem .6rem;font-family:var(--xmc-font-mono)">
                ${r.result?.delta != null ? (r.result.delta > 0 ? "+" : "") + r.result.delta.toFixed(3) : "—"}
              </td>
            </tr>
          `)}
        </tbody>
      </table>

      ${detail ? html`
        <div style="margin-top:1rem;padding:1rem;border:1px solid var(--color-border);border-radius:6px;background:var(--color-surface)">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.5rem">
            <strong>实验详情</strong>
            <button type="button" onClick=${() => setDetail(null)} style="font-size:.75rem">关闭</button>
          </div>
          <div style="font-size:.82rem;line-height:1.6">
            <div><strong>ID:</strong> <code>${detail.id}</code></div>
            <div><strong>假设:</strong> ${detail.hypothesis}</div>
            <div><strong>Metric:</strong> ${detail.metric}</div>
            ${detail.result ? html`
              <div><strong>Decision:</strong> ${detail.result.decision}</div>
              <div><strong>Delta:</strong> ${detail.result.delta != null ? detail.result.delta.toFixed(4) : "—"}</div>
              <div><strong>p-value:</strong> ${detail.result.delta_p_value != null ? detail.result.delta_p_value.toFixed(4) : "—"}</div>
              <div><strong>Baseline:</strong> ${detail.result.baseline_value != null ? detail.result.baseline_value.toFixed(4) : "—"}
                (n=${detail.result.n_baseline || 0})</div>
              <div><strong>Treatment:</strong> ${detail.result.treatment_value != null ? detail.result.treatment_value.toFixed(4) : "—"}
                (n=${detail.result.n_treatment || 0})</div>
              ${detail.result.decision_reason ? html`<div><strong>Reason:</strong> ${detail.result.decision_reason}</div>` : null}
            ` : html`<div style="opacity:.7">实验进行中…</div>`}
          </div>
        </div>
      ` : null}
    </div>
  `;
}
