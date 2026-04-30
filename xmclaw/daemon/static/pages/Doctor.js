// XMclaw — Doctor page
//
// Two views in one page:
//   1. Live daemon status (auto-refresh every 5s) — Daemon health,
//      Agent / LLM, auth, tool count, MCP. Quick "is it alive" loop.
//   2. B-102: full doctor pipeline run via POST /api/v2/doctor/run.
//      Runs all 21 registered checks, shows per-check ok/advisory/
//      fix_available. "运行诊断" / "自动修复" buttons trigger
//      the daemon-side pipeline so users don't need a terminal.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet, apiPost } from "../lib/api.js";
import { toast } from "../lib/toast.js";

export function DoctorPage({ token }) {
  const [data, setData] = useState(null);
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);
  // B-102: full doctor run state.
  const [report, setReport] = useState(null);
  const [running, setRunning] = useState(false);
  const [fixing, setFixing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [s, h] = await Promise.all([
          apiGet("/api/v2/status", token),
          apiGet("/health", token),
        ]);
        if (cancelled) return;
        setData(s);
        setHealth(h);
        setError(null);
      } catch (exc) {
        if (!cancelled) setError(String(exc.message || exc));
      }
    }
    load();
    const id = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [token]);

  const runDoctor = async (applyFix) => {
    if (applyFix) setFixing(true); else setRunning(true);
    try {
      const r = await apiPost("/api/v2/doctor/run", { fix: !!applyFix }, token);
      setReport(r);
      const fixed = (r.summary && r.summary.fixes_applied) || [];
      if (applyFix) {
        toast.success(
          fixed.length > 0
            ? `已应用 ${fixed.length} 项 fix：${fixed.join(", ")}`
            : "无可自动修复的项",
        );
      } else {
        toast.info(`完成：${r.summary.ok}/${r.summary.total} OK`);
      }
    } catch (exc) {
      toast.error("运行失败：" + (exc.message || exc));
    } finally {
      setRunning(false);
      setFixing(false);
    }
  };

  if (error) {
    return html`
      <section class="xmc-datapage">
        <h2>诊断</h2>
        <p class="xmc-datapage__error">${error}</p>
      </section>
    `;
  }
  if (!data || !health) {
    return html`<section class="xmc-datapage"><p>加载中…</p></section>`;
  }

  const checks = [
    { label: "Daemon 健康", ok: health.status === "ok", detail: `version ${health.version}` },
    { label: "Agent 已绑定 LLM", ok: !!data.agent_wired, detail: data.model || "未绑定" },
    { label: "鉴权", ok: !!data.auth_required, detail: data.auth_required ? "pairing token" : "no-auth" },
    { label: "工具", ok: (data.tools || []).length > 0, detail: `${(data.tools || []).length} 个` },
    { label: "MCP 服务", ok: (data.mcp_servers || []).length > 0, detail: (data.mcp_servers || []).join(", ") || "未配置" },
  ];

  return html`
    <section class="xmc-datapage" aria-labelledby="doctor-title">
      <header class="xmc-datapage__header">
        <h2 id="doctor-title">诊断</h2>
        <p class="xmc-datapage__subtitle">运行中的 daemon 自检（每 5 秒刷新）。</p>
      </header>
      <ul class="xmc-datapage__list">
        ${checks.map((c) => html`
          <li class="xmc-datapage__row" key=${c.label}>
            <div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem">
              <strong>${c.label}</strong>
              <${Badge} tone=${c.ok ? "success" : "warn"}>${c.ok ? "OK" : "异常"}</${Badge}>
            </div>
            <small>${c.detail}</small>
          </li>
        `)}
      </ul>

      <!-- B-102: full doctor pipeline -->
      <header class="xmc-datapage__header" style="margin-top:1.5rem">
        <h3>完整诊断（${"21"} 项检查）</h3>
        <p class="xmc-datapage__subtitle">
          跟 <code>xmclaw doctor</code> 命令同款的全套检查，包括 config /
          memory / persona / pairing / events / skill runtime / 等。
          某些项支持自动修复（"自动修复"按钮会逐项尝试）。
        </p>
        <div style="display:flex;gap:.5rem;margin-top:.6rem">
          <button
            type="button"
            class="xmc-h-btn xmc-h-btn--primary"
            onClick=${() => runDoctor(false)}
            disabled=${running || fixing}
          >${running ? "运行中…" : "运行诊断"}</button>
          <button
            type="button"
            class="xmc-h-btn xmc-h-btn--ghost"
            onClick=${() => runDoctor(true)}
            disabled=${running || fixing}
            title="先运行所有检查，再对支持 fix 的失败项调 fix() 方法"
          >${fixing ? "修复中…" : "自动修复"}</button>
        </div>
      </header>
      ${report ? html`
        <div style="margin-top:.8rem">
          <div style="display:flex;gap:.5rem;align-items:center;font-size:.85rem;margin-bottom:.5rem">
            <strong>结果：</strong>
            <${Badge} tone="success">${report.summary.ok} OK</${Badge}>
            ${report.summary.failed > 0
              ? html`<${Badge} tone="warn">${report.summary.failed} 异常</${Badge}>`
              : null}
            ${(report.summary.fixes_applied || []).length > 0
              ? html`<${Badge} tone="info">${report.summary.fixes_applied.length} 已修</${Badge}>`
              : null}
          </div>
          <ul class="xmc-datapage__list">
            ${report.results.map((r) => html`
              <li
                class="xmc-datapage__row"
                key=${r.id || r.name}
                style=${"border-left:3px solid " + (r.ok ? "var(--xmc-success, #6ac88a)" : (r.advisory ? "var(--xmc-warn, #c8a86a)" : "var(--xmc-error, #e77f7f)"))}
              >
                <div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem">
                  <strong>${r.name}</strong>
                  <${Badge} tone=${r.ok ? "success" : (r.advisory ? "warn" : "danger")}>
                    ${r.ok ? "OK" : (r.advisory ? "建议" : "失败")}
                  </${Badge}>
                </div>
                <small style="display:block;margin-top:.2rem">${r.detail}</small>
                ${r.advisory ? html`
                  <small style="display:block;margin-top:.3rem;padding:.3rem .5rem;background:rgba(200,168,106,.1);border-radius:3px;font-size:.74rem">
                    💡 ${r.advisory}
                  </small>
                ` : null}
                ${r.fix_available ? html`
                  <small style="display:block;margin-top:.2rem;font-size:.7rem;color:var(--xmc-info, #6aa3f0)">
                    ⚡ 这一项支持自动修复 — 点上方"自动修复"
                  </small>
                ` : null}
              </li>
            `)}
          </ul>
        </div>
      ` : null}
    </section>
  `;
}
