// XMclaw — Backup page
//
// B-103: full UI parity with the ``xmclaw backup`` CLI. Reads from
// /api/v2/backup (list / info / verify / restore / delete / prune)
// + the per-backup-event stream from /api/v2/events.
//
// Previous version was read-only — showed config policy + recent
// events, told users to drop into a terminal to actually back
// anything up. The full backup module (xmclaw/backup/*) was wired
// at the CLI but had no router until B-103.

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { Badge } from "../components/atoms/badge.js";
import { apiGet, apiPost, apiDelete } from "../lib/api.js";
import { confirmDialog } from "../lib/dialog.js";
import { toast } from "../lib/toast.js";

function _humanBytes(n) {
  if (!n || n < 1024) return `${n || 0} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function _humanTime(ts) {
  if (!ts) return "";
  const t = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  if (isNaN(t)) return String(ts);
  return t.toLocaleString();
}

export function BackupPage({ token }) {
  const [config, setConfig] = useState(null);
  const [events, setEvents] = useState(null);
  const [backups, setBackups] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);  // backup name currently being acted on
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");

  const reload = useCallback(() => {
    const BACKUP_TYPES = new Set(["backup_started", "backup_finished", "backup_failed"]);
    Promise.all([
      apiGet("/api/v2/config", token),
      apiGet("/api/v2/events?limit=200", token).catch(() => ({ events: [] })),
      apiGet("/api/v2/backup", token).catch((e) => ({ backups: [], error: e.message })),
    ])
      .then(([cfg, evs, bks]) => {
        setConfig(cfg.config || {});
        setEvents((evs.events || []).filter((e) => BACKUP_TYPES.has(e.type)));
        setBackups(bks.backups || []);
        setError(null);
      })
      .catch((e) => setError(String(e.message || e)));
  }, [token]);

  useEffect(() => { reload(); }, [reload]);

  const onCreate = async () => {
    setCreating(true);
    try {
      const r = await apiPost("/api/v2/backup", { name: newName.trim() || undefined }, token);
      toast.success(`已备份：${r.name}（${r.files_count} 个文件 · ${_humanBytes(r.total_bytes)}）`);
      setNewName("");
      reload();
    } catch (e) {
      toast.error("备份失败：" + (e.message || e));
    } finally {
      setCreating(false);
    }
  };

  const onVerify = async (name) => {
    setBusy(name);
    try {
      const r = await apiPost(`/api/v2/backup/${encodeURIComponent(name)}/verify`, {}, token);
      if (r.verified) toast.success(`${name} 校验通过（sha256 一致）`);
      else toast.error(`${name} 校验失败：${r.error || "归档已损坏"}`);
    } catch (e) {
      toast.error("校验失败：" + (e.message || e));
    } finally {
      setBusy(null);
    }
  };

  const onRestore = async (name) => {
    const ok = await confirmDialog({
      title: `恢复 ${name}？`,
      body: `当前 ~/.xmclaw 会被原子替换为这个备份的内容。\n` +
            `daemon 必须随后重启才能加载恢复后的 events.db / sessions.db。\n` +
            `失败时会自动 rollback 到 .prev-<时间戳>。`,
      confirmLabel: "恢复",
      confirmTone: "danger",
    });
    if (!ok) return;
    setBusy(name);
    try {
      await apiPost(`/api/v2/backup/${encodeURIComponent(name)}/restore`, {}, token);
      toast.success(`${name} 已恢复 — 请重启 daemon`);
    } catch (e) {
      toast.error("恢复失败：" + (e.message || e));
    } finally {
      setBusy(null);
    }
  };

  const onDelete = async (name) => {
    const ok = await confirmDialog({
      title: `删除 ${name}？`,
      body: `备份归档 + manifest 都会从磁盘删除。不可恢复。`,
      confirmLabel: "删除",
      confirmTone: "danger",
    });
    if (!ok) return;
    setBusy(name);
    try {
      await apiDelete(`/api/v2/backup/${encodeURIComponent(name)}`, token);
      toast.success(`${name} 已删除`);
      reload();
    } catch (e) {
      toast.error("删除失败：" + (e.message || e));
    } finally {
      setBusy(null);
    }
  };

  const onPrune = async () => {
    const ok = await confirmDialog({
      title: "Prune 旧备份",
      body: `只保留最新的 backup.keep 个 auto-* 备份（手动命名的备份不动）。`,
      confirmLabel: "Prune",
    });
    if (!ok) return;
    try {
      const r = await apiPost("/api/v2/backup/prune", {}, token);
      toast.success(`已删除 ${r.removed_count} 个旧备份`);
      reload();
    } catch (e) {
      toast.error("Prune 失败：" + (e.message || e));
    }
  };

  if (error) return html`<section class="xmc-datapage"><h2>备份</h2><p class="xmc-datapage__error">${error}</p></section>`;
  if (!config) return html`<section class="xmc-datapage"><p>加载中…</p></section>`;

  const policy = (config && config.backup) || {};
  const enabled = !!policy.auto_daily;

  return html`
    <section class="xmc-datapage" aria-labelledby="backup-title">
      <header class="xmc-datapage__header">
        <h2 id="backup-title">备份</h2>
        <p class="xmc-datapage__subtitle">
          工作区备份策略 + 现有备份列表 + 创建/校验/恢复/删除。
          每个备份是 tar.gz 归档 + manifest（含 sha256 完整性校验）。
        </p>
      </header>

      <div class="xmc-datapage__row" style="margin-bottom:1rem">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <strong>每日自动备份</strong>
          <${Badge} tone=${enabled ? "success" : "muted"}>${enabled ? "已开启" : "未开启"}</${Badge}>
        </div>
        ${policy.keep ? html`<small>保留：最新 ${policy.keep} 个 auto-* 备份</small>` : null}
        ${policy.interval_s ? html`<small>间隔：${policy.interval_s}s</small>` : null}
      </div>

      <div style="display:flex;gap:.5rem;align-items:center;margin-bottom:1rem;flex-wrap:wrap">
        <input
          type="text"
          placeholder="备份名（留空自动用 auto-YYYY-MM-DD-HHMMSS）"
          value=${newName}
          onInput=${(e) => setNewName(e.target.value)}
          onKeyDown=${(e) => { if (e.key === "Enter") onCreate(); }}
          style="flex:1 1 280px;min-width:0;padding:.4rem .6rem;font-family:var(--xmc-font-mono);font-size:.85rem"
        />
        <button type="button" class="xmc-h-btn xmc-h-btn--primary" onClick=${onCreate} disabled=${creating}>
          ${creating ? "备份中…" : "立即备份"}
        </button>
        <button type="button" class="xmc-h-btn xmc-h-btn--ghost" onClick=${onPrune} disabled=${creating}>
          Prune
        </button>
      </div>

      <h3 style="margin:1rem 0 .5rem">现有备份（${(backups || []).length}）</h3>
      ${(backups || []).length === 0
        ? html`<p class="xmc-datapage__empty">还没有任何备份。点上方"立即备份"创建第一个。</p>`
        : html`
            <ul class="xmc-datapage__list">
              ${backups.map((b) => html`
                <li class="xmc-datapage__row" key=${b.name}>
                  <div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem;flex-wrap:wrap">
                    <strong>${b.name}</strong>
                    <span class="xmc-datapage__subtitle" style="font-size:.78rem">
                      ${b.files_count} 文件 · ${_humanBytes(b.total_bytes)} ·
                      ${_humanTime(b.created_at)}
                    </span>
                  </div>
                  ${b.archive_sha256
                    ? html`<small style="display:block;font-size:.7rem;color:var(--xmc-fg-muted);font-family:var(--xmc-font-mono)">sha256 ${b.archive_sha256.slice(0, 16)}…</small>`
                    : null}
                  <div style="display:flex;gap:.3rem;margin-top:.4rem;flex-wrap:wrap">
                    <button type="button" class="xmc-h-btn xmc-h-btn--ghost"
                      style="font-size:.72rem;padding:.2rem .5rem"
                      onClick=${() => onVerify(b.name)} disabled=${busy === b.name}>校验</button>
                    <button type="button" class="xmc-h-btn xmc-h-btn--primary"
                      style="font-size:.72rem;padding:.2rem .5rem"
                      onClick=${() => onRestore(b.name)} disabled=${busy === b.name}>恢复</button>
                    <button type="button" class="xmc-h-btn xmc-h-btn--ghost"
                      style="font-size:.72rem;padding:.2rem .5rem;color:var(--xmc-error,#e77f7f)"
                      onClick=${() => onDelete(b.name)} disabled=${busy === b.name}>删除</button>
                  </div>
                </li>
              `)}
            </ul>
          `}

      <h3 style="margin:1.5rem 0 .5rem">最近事件</h3>
      ${(events || []).length === 0
        ? html`<p class="xmc-datapage__empty" style="font-size:.78rem">尚无备份事件 — 自动备份开关在 <code>backup.auto_daily</code></p>`
        : html`
            <ul class="xmc-datapage__list">
              ${events.slice().reverse().slice(0, 30).map((e) => {
                const ts = _humanTime(e.ts);
                const tone = e.type === "backup_failed" ? "error"
                  : e.type === "backup_finished" ? "success" : "muted";
                return html`
                  <li class="xmc-datapage__row" key=${e.id || ts}>
                    <div style="display:flex;justify-content:space-between;gap:.5rem">
                      <${Badge} tone=${tone}>${e.type}</${Badge}>
                      <small>${ts}</small>
                    </div>
                    <code style="font-size:.7rem">${JSON.stringify(e.payload).slice(0, 160)}</code>
                  </li>
                `;
              })}
            </ul>
          `}
    </section>
  `;
}
