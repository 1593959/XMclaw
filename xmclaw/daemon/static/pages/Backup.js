// XMclaw — Backup page
//
// Reads the backup section from /api/v2/config and shows the policy + most
// recent backup events. Triggering a manual backup is CLI-only today
// (`xmclaw backup create`) — this page is read-only until we expose a write
// endpoint.

const { h } = window.__xmc.preact;
const { useState, useEffect } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet } from "../lib/api.js";

export function BackupPage({ token }) {
  const [config, setConfig] = useState(null);
  const [events, setEvents] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const BACKUP_TYPES = new Set(["backup_started", "backup_finished", "backup_failed"]);
    Promise.all([
      apiGet("/api/v2/config", token),
      apiGet("/api/v2/events?limit=200", token).catch(() => ({ events: [] })),
    ])
      .then(([cfg, evs]) => {
        if (cancelled) return;
        setConfig(cfg.config || {});
        const all = evs.events || [];
        setEvents(all.filter((e) => BACKUP_TYPES.has(e.type)));
      })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [token]);

  if (error) return html`<section class="xmc-datapage"><h2>备份</h2><p class="xmc-datapage__error">${error}</p></section>`;
  if (!config) return html`<section class="xmc-datapage"><p>加载中…</p></section>`;

  const policy = (config && config.backup) || {};
  const enabled = !!policy.auto_daily;

  return html`
    <section class="xmc-datapage" aria-labelledby="backup-title">
      <header class="xmc-datapage__header">
        <h2 id="backup-title">备份</h2>
        <p class="xmc-datapage__subtitle">
          工作区备份策略与最近事件。手动触发：终端运行 <code>xmclaw backup create</code>。
        </p>
      </header>

      <div class="xmc-datapage__row" style="margin-bottom:1rem">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <strong>每日自动备份</strong>
          <${Badge} tone=${enabled ? "success" : "muted"}>${enabled ? "已开启" : "未开启"}</${Badge}>
        </div>
        ${policy.retention_days ? html`<small>保留天数：${policy.retention_days}</small>` : null}
        ${policy.dest ? html`<small>目标：<code>${policy.dest}</code></small>` : null}
      </div>

      <h3 style="margin:1rem 0 .5rem">最近事件</h3>
      ${(events || []).length === 0
        ? html`<p class="xmc-datapage__empty">尚无备份事件（功能未运行或未启用）</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${events.slice().reverse().map((e) => {
                const ts = e.ts ? new Date(e.ts * 1000).toLocaleString() : "";
                const tone = e.type === "backup_failed" ? "error"
                  : e.type === "backup_finished" ? "success" : "muted";
                return html`
                  <li class="xmc-datapage__row" key=${e.id || ts}>
                    <div style="display:flex;justify-content:space-between;gap:.5rem">
                      <${Badge} tone=${tone}>${e.type}</${Badge}>
                      <small>${ts}</small>
                    </div>
                    <code>${JSON.stringify(e.payload).slice(0, 160)}</code>
                  </li>
                `;
              })}
            </ul>
          `}
    </section>
  `;
}
