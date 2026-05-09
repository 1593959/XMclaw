// XMclaw — MarketplacePage (B-390 Sprint 2).
//
// Browses the curated skill catalog hosted on GitHub raw, lets the
// user one-click install / remove. Backed by the daemon router at
// `/api/v2/skills/marketplace` so we share install logic with the
// `xmclaw skill install` CLI.
//
// Design choices:
// - Search filters client-side against the already-loaded index. The
//   catalog is small (5-50 entries during MVP); a server round-trip
//   per keystroke is overkill.
// - Install is a single POST; we then re-fetch the installed list so
//   the row's button flips to "Installed". On success we show a toast
//   reminding the user that daemon restart picks up the new skill.
// - Trust tier badges: green for "verified", amber for "community".
//   Doesn't gate install — it just sets expectations.
// - Stays under the 500-line UI budget (FRONTEND_DESIGN.md §1.4).

const { h } = window.__xmc.preact;
const { useState, useEffect, useCallback, useMemo } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet, apiSend } from "../lib/api.js";
import { confirmDialog } from "../lib/dialog.js";
import { toast } from "../lib/toast.js";

function Icon({ d }) {
  return html`
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
         class="xmc-icon" aria-hidden="true">
      <path d=${d} />
    </svg>
  `;
}

const I_STORE  = "M3 9h18l-1.5 11a2 2 0 0 1-2 2h-11a2 2 0 0 1-2-2zM9 9V5a3 3 0 1 1 6 0v4";
const I_SEARCH = "M11 17a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM21 21l-4.3-4.3";
const I_REFRESH = "M21 12a9 9 0 1 1-3-6.7L21 8 M21 3v5h-5";
const I_CHECK = "M20 6 9 17l-5-5";
const I_TRASH = "M3 6h18 M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2 M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6";

function Badge({ tone, children, title }) {
  const tones = {
    success: "background:rgba(34,197,94,.18);color:#7ce29c;border:1px solid rgba(34,197,94,.35)",
    info:    "background:rgba(59,130,246,.18);color:#93c5fd;border:1px solid rgba(59,130,246,.35)",
    warn:    "background:rgba(234,179,8,.18);color:#fde68a;border:1px solid rgba(234,179,8,.35)",
    muted:   "background:rgba(148,163,184,.18);color:#cbd5e1;border:1px solid rgba(148,163,184,.30)",
  };
  const css = tones[tone] || tones.muted;
  return html`
    <span title=${title || ""}
          style=${`display:inline-block;padding:1px 7px;border-radius:10px;font-size:.65rem;letter-spacing:.04em;${css}`}>
      ${children}
    </span>
  `;
}

function CatalogCard({ skill, isInstalled, busy, onInstall, onRemove }) {
  const tier = skill.trust_tier || "community";
  const tierTone = tier === "verified" ? "success" : "muted";
  return html`
    <article style="padding:.75rem .9rem;border:1px solid var(--color-border);border-radius:8px;background:color-mix(in srgb, var(--midground) 4%, transparent)">
      <header style="display:flex;justify-content:space-between;align-items:baseline;gap:.6rem;flex-wrap:wrap">
        <span>
          <strong style="font-size:1.02rem">${skill.name}</strong>
          <code style="margin-left:.45rem;font-size:.72rem;color:var(--xmc-fg-muted)">${skill.id}</code>
        </span>
        <span style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap">
          <${Badge} tone=${tierTone} title=${tier === "verified" ? "由 XMclaw 团队审核" : "社区贡献"}>
            ${tier.toUpperCase()}
          </${Badge}>
          <code style="font-size:.7rem;color:var(--xmc-fg-muted)">v${skill.version}</code>
        </span>
      </header>
      ${skill.description
        ? html`<p style="margin:.4rem 0 .35rem 0;font-size:.85rem;color:var(--xmc-fg-muted);line-height:1.4">
            ${skill.description}
          </p>`
        : null}
      <div style="display:flex;gap:.4rem;flex-wrap:wrap;margin:.35rem 0;font-size:.7rem;color:var(--xmc-fg-muted)">
        ${(skill.tags || []).map((t) => html`<span style="padding:1px 6px;border-radius:3px;background:rgba(120,120,120,.18)">${t}</span>`)}
        <span>· ${skill.author || "?"}</span>
        <span>· ${skill.license || "?"}</span>
        ${skill.install_size_kb ? html`<span>· ${skill.install_size_kb} KB</span>` : null}
      </div>
      <footer style="display:flex;gap:.4rem;margin-top:.5rem;align-items:center">
        ${isInstalled
          ? html`
            <button type="button" disabled
                    style="padding:.3rem .8rem;border-radius:4px;background:rgba(34,197,94,.2);border:1px solid rgba(34,197,94,.5);color:#7ce29c;cursor:default;font-size:.78rem;display:inline-flex;align-items:center;gap:.3rem">
              <${Icon} d=${I_CHECK} /> 已安装
            </button>
            <button type="button" onClick=${() => onRemove(skill)} disabled=${busy}
                    style="padding:.3rem .8rem;border-radius:4px;background:transparent;border:1px solid var(--color-border);color:var(--color-destructive,#c66);cursor:pointer;font-size:.78rem;display:inline-flex;align-items:center;gap:.3rem">
              <${Icon} d=${I_TRASH} /> 卸载
            </button>`
          : html`
            <button type="button" onClick=${() => onInstall(skill)} disabled=${busy}
                    style="padding:.35rem .9rem;border-radius:4px;background:var(--color-primary,#193);color:#fff;border:none;cursor:pointer;font-size:.8rem">
              ${busy ? "安装中..." : "安装"}
            </button>`}
        <a href=${skill.source.startsWith("github:") ? "https://github.com/" + skill.source.slice(7) : "#"}
           target="_blank" rel="noopener"
           style="margin-left:auto;font-size:.72rem;color:var(--xmc-fg-muted);text-decoration:none">
          source ↗
        </a>
      </footer>
    </article>
  `;
}

export function MarketplacePage({ token }) {
  const [index, setIndex] = useState(null);
  const [installed, setInstalled] = useState({});  // id -> InstalledSkill
  const [search, setSearch] = useState("");
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState(null);

  const loadAll = useCallback(async (refresh) => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const path = refresh
        ? "/api/v2/skills/marketplace?refresh=1"
        : "/api/v2/skills/marketplace";
      const [mk, inst] = await Promise.all([
        apiGet(path, token),
        apiGet("/api/v2/skills/installed", token),
      ]);
      const idx = mk && mk.index ? mk.index : { skills: [] };
      setIndex(idx);
      const map = {};
      for (const r of (inst && inst.skills) || []) map[r.id] = r;
      setInstalled(map);
    } catch (e) {
      if (!e.tokenNotReady) setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { loadAll(false); }, [loadAll]);

  const filtered = useMemo(() => {
    if (!index) return [];
    const q = search.trim().toLowerCase();
    if (!q) return index.skills;
    return index.skills.filter((s) => {
      const haystack = [
        s.id, s.name, s.description, s.author,
        ...(s.tags || []),
      ].join(" ").toLowerCase();
      return haystack.includes(q);
    });
  }, [index, search]);

  const onInstall = useCallback(async (skill) => {
    const ok = await confirmDialog({
      title: `安装 ${skill.name}?`,
      message: `即将从 ${skill.source} 克隆到 ~/.xmclaw/skills_user/${skill.id}/，并跑安全扫描。`,
      confirmText: "安装",
    });
    if (!ok) return;
    setBusyId(skill.id);
    try {
      const r = await apiSend("POST", "/api/v2/skills/install", { id: skill.id }, token);
      if (r && r.ok) {
        const findings = (r.findings || []).length;
        toast.success(
          `${skill.id} 安装成功 — 重启 daemon 后生效${findings ? ` (${findings} 条非致命安全提示)` : ""}`,
          { ttl: 6000 },
        );
        await loadAll(false);
      } else {
        toast.error("安装失败：" + ((r && r.error) || "unknown"), { ttl: 6000 });
      }
    } catch (e) {
      toast.error("安装失败：" + (e.message || String(e)), { ttl: 6000 });
    } finally {
      setBusyId(null);
    }
  }, [token, loadAll]);

  const onRemove = useCallback(async (skill) => {
    const ok = await confirmDialog({
      title: `卸载 ${skill.name}?`,
      message: `将删除 ~/.xmclaw/skills_user/${skill.id}/`,
      confirmText: "卸载",
    });
    if (!ok) return;
    setBusyId(skill.id);
    try {
      await apiSend("DELETE", `/api/v2/skills/installed/${encodeURIComponent(skill.id)}`, null, token);
      toast.success(`${skill.id} 已卸载 — 重启 daemon 后生效`);
      await loadAll(false);
    } catch (e) {
      toast.error("卸载失败：" + (e.message || String(e)));
    } finally {
      setBusyId(null);
    }
  }, [token, loadAll]);

  if (loading && !index) {
    return html`
      <section style="padding:1rem;color:var(--xmc-fg-muted)">
        <h2 style="display:flex;gap:.5rem;align-items:center"><${Icon} d=${I_STORE} /> 技能商店</h2>
        <p>正在加载目录…</p>
      </section>
    `;
  }

  return html`
    <section style="padding:1rem">
      <header style="display:flex;justify-content:space-between;align-items:baseline;gap:.6rem;flex-wrap:wrap">
        <span style="display:flex;gap:.5rem;align-items:center">
          <h2 style="margin:0;display:flex;gap:.5rem;align-items:center"><${Icon} d=${I_STORE} /> 技能商店</h2>
          <code style="font-size:.7rem;color:var(--xmc-fg-muted)">B-390 MVP · curated</code>
        </span>
        <span style="display:flex;gap:.4rem">
          <button type="button" onClick=${() => loadAll(true)} disabled=${loading}
                  style="padding:.3rem .7rem;border-radius:4px;background:transparent;border:1px solid var(--color-border);color:var(--xmc-fg);cursor:pointer;font-size:.78rem;display:inline-flex;align-items:center;gap:.3rem">
            <${Icon} d=${I_REFRESH} /> 刷新
          </button>
        </span>
      </header>

      <p style="margin:.4rem 0 .6rem 0;color:var(--xmc-fg-muted);font-size:.85rem">
        从社区策展目录安装技能；安装后重启 daemon (<code>xmclaw restart</code>) 即可使用。
        ${index && index.updated ? html`目录更新于 ${index.updated}。` : null}
      </p>

      <div style="margin:.6rem 0;display:flex;gap:.5rem;align-items:center">
        <span style="display:inline-flex;align-items:center"><${Icon} d=${I_SEARCH} /></span>
        <input type="text" value=${search}
               onInput=${(e) => setSearch(e.target.value)}
               placeholder="按名称 / 描述 / 标签 / 作者 过滤"
               style="flex:1;padding:.4rem .6rem;border:1px solid var(--color-border);border-radius:4px;background:transparent;color:var(--xmc-fg);font-size:.85rem" />
        <span style="font-size:.75rem;color:var(--xmc-fg-muted)">${filtered.length} / ${index ? index.skills.length : 0}</span>
      </div>

      ${error
        ? html`
          <div style="padding:.6rem .75rem;margin:.5rem 0;border:1px solid rgba(220,38,38,.4);border-radius:4px;background:rgba(220,38,38,.1);color:#fbb;font-size:.85rem">
            目录加载失败：${error}
          </div>`
        : null}

      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(360px, 1fr));gap:.6rem;margin-top:.5rem">
        ${filtered.map((s) => html`
          <${CatalogCard}
            key=${s.id}
            skill=${s}
            isInstalled=${!!installed[s.id]}
            busy=${busyId === s.id}
            onInstall=${onInstall}
            onRemove=${onRemove}
          />
        `)}
      </div>

      ${filtered.length === 0 && !loading
        ? html`<p style="text-align:center;color:var(--xmc-fg-muted);padding:1rem">没有匹配项</p>`
        : null}
    </section>
  `;
}
