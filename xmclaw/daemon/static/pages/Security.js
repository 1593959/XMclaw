// XMclaw — Security page
//
// Pending-approval queue from /api/v2/approvals. Polls so a request raised
// by a tool turn shows up live without a refresh. approve/deny call the
// per-id endpoints; the daemon decides what happens next.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Button } from "../components/atoms/button.js";
import { Badge } from "../components/atoms/badge.js";
import { apiGet, apiPost } from "../lib/api.js";

export function SecurityPage({ token }) {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);

  async function refresh(signal) {
    try {
      const d = await apiGet("/api/v2/approvals", token);
      if (signal && signal.cancelled) return;
      const list = Array.isArray(d) ? d : (d && d.requests) || [];
      setItems(list);
      setError(null);
    } catch (exc) {
      if (signal && signal.cancelled) return;
      setError(String(exc.message || exc));
    }
  }

  useEffect(() => {
    const signal = { cancelled: false };
    refresh(signal);
    const id = setInterval(() => refresh(signal), 4000);
    return () => { signal.cancelled = true; clearInterval(id); };
  }, [token]);

  async function decide(reqId, verb) {
    setBusy(reqId + ":" + verb);
    try {
      await apiPost(`/api/v2/approvals/${encodeURIComponent(reqId)}/${verb}`, {}, token);
      await refresh();
    } catch (exc) {
      setError(String(exc.message || exc));
    } finally {
      setBusy(null);
    }
  }

  if (error) return html`<section class="xmc-datapage"><h2>安全</h2><p class="xmc-datapage__error">${error}</p></section>`;
  if (!items) return html`<section class="xmc-datapage"><p>加载中…</p></section>`;

  return html`
    <section class="xmc-datapage" aria-labelledby="sec-title">
      <header class="xmc-datapage__header">
        <h2 id="sec-title">安全</h2>
        <p class="xmc-datapage__subtitle">待审批请求 ${items.length} 条（每 4 秒刷新）。</p>
      </header>
      ${items.length === 0
        ? html`<p class="xmc-datapage__empty">没有待审批的工具调用 ✓</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${items.map((it) => {
                const id = it.id || it.request_id;
                return html`
                  <li class="xmc-datapage__row" key=${id}>
                    <div style="display:flex;justify-content:space-between;gap:.5rem">
                      <strong>${it.tool_name || it.name || "工具"}</strong>
                      <${Badge} tone="warn">待审批</${Badge}>
                    </div>
                    ${it.reason ? html`<small>${it.reason}</small>` : null}
                    ${it.args ? html`<code>${JSON.stringify(it.args).slice(0, 200)}</code>` : null}
                    <div style="display:flex;gap:.5rem;margin-top:.25rem">
                      <${Button}
                        variant="primary" size="sm"
                        disabled=${busy === id + ":approve"}
                        onClick=${() => decide(id, "approve")}
                      >通过</${Button}>
                      <${Button}
                        variant="danger" size="sm"
                        disabled=${busy === id + ":deny"}
                        onClick=${() => decide(id, "deny")}
                      >拒绝</${Button}>
                    </div>
                  </li>
                `;
              })}
            </ul>
          `}
    </section>
  `;
}
