// XMclaw — SkillsPage 1:1 layout port of hermes-agent SkillsPage.tsx
//
// Hermes layout: sticky left filter panel (view switch + categories) +
// content area with skill cards. We reuse the visual structure but
// adapt the content to XMclaw's skill-versioning data:
//   - View switch: All / Built-in / User
//   - Cards show skill id (mono), source badge, head version, version
//     ladder (each version with "head" badge if active)
//
// Data: GET /api/v2/skills returns {skills: [{id, head_version, source,
// versions: [{version, is_head, manifest}]}], evolution_enabled}.
//
// Port file:line refs: SkillsPage.tsx:253-348 (filter panel layout),
// 350-450 (search results card), 451-580 (category-grouped list).

const { h } = window.__xmc.preact;
const { useState, useEffect, useMemo } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet, apiPost } from "../lib/api.js";
import { confirmDialog } from "../lib/dialog.js";
import { toast } from "../lib/toast.js";

// Inline SVG (lucide-react equivalents).
function Icon({ d, className }) {
  return html`
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"
         class=${"xmc-icon " + (className || "")} aria-hidden="true">
      <path d=${d} />
    </svg>
  `;
}

const I_PACKAGE = "M16.5 9.4 7.55 4.24M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16zM3.27 6.96 12 12.01l8.73-5.05M12 22.08V12";
const I_SEARCH  = "M11 17a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM21 21l-4.3-4.3";
const I_X       = "M18 6 6 18 M6 6l12 12";
const I_FILTER  = "M22 3H2l8 9.46V19l4 2v-8.54z";
const I_SPARKLES = "m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3z";

// ── Panel item (left-rail option button) ─────────────────────────

// B-161: 外部技能导入面板。列出 ~/.agents/skills/ + ~/.claude/skills/
// 下的 SKILL.md，让用户挑选 import 进 XMclaw 私有目录。
// 改装路径策略：默认不扫共享目录，避免跨 agent 信任污染。
function ImportExternalSkills({ token, onImported }) {
  const [data, setData] = useState(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(null);

  const load = () => {
    apiGet("/api/v2/auto_evo/learned_skills/discoverable", token)
      .then((d) => setData(d))
      .catch(() => setData({ candidates: [] }));
  };

  useEffect(() => {
    if (open && data === null) load();
  }, [open]);

  const onImport = async (sourcePath) => {
    setBusy(sourcePath);
    try {
      const url = "/api/v2/auto_evo/learned_skills/import"
        + (token ? `?token=${encodeURIComponent(token)}` : "");
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_path: sourcePath }),
      });
      const d = await r.json();
      if (!r.ok || d.error || d.ok === false) throw new Error(d.error || `HTTP ${r.status}`);
      toast.success("已导入");
      load();
      if (onImported) onImported();
    } catch (e) {
      toast.error("导入失败：" + (e.message || e));
    } finally {
      setBusy(null);
    }
  };

  const candidates = data?.candidates || [];
  const importable = candidates.filter((c) => !c.already_imported);

  return html`
    <details
      open=${open}
      onToggle=${(e) => setOpen(e.target.open)}
      style="margin:.5rem 0;padding:.5rem .7rem;border:1px solid var(--color-border);border-radius:6px;background:color-mix(in srgb, var(--midground) 4%, transparent)"
    >
      <summary style="cursor:pointer;font-size:.85rem">
        <strong>📥 从其他 agent 路径导入技能</strong>
        ${candidates.length
          ? html`<span class="xmc-h-badge xmc-h-badge--info" style="margin-left:.4rem">发现 ${candidates.length} 个</span>`
          : null}
        <small style="opacity:.7;margin-left:.4rem">扫 ~/.agents/skills/ + ~/.claude/skills/，挑选要的导进 XMclaw 私有目录</small>
      </summary>
      ${data === null
        ? html`<p style="margin:.5rem 0;font-size:.78rem;opacity:.7">加载中…</p>`
        : candidates.length === 0
          ? html`<p style="margin:.5rem 0;font-size:.78rem;opacity:.7">~/.agents/skills/ 和 ~/.claude/skills/ 都为空。装新技能可用 <code>npx skills add &lt;url&gt;</code> 后回这里导入。</p>`
          : html`<ul style="list-style:none;padding:0;margin:.5rem 0 0;display:grid;gap:.3rem">
              ${candidates.map((c) => html`
                <li key=${c.source_path} style="display:flex;align-items:center;gap:.5rem;padding:.4rem .55rem;border:1px solid var(--color-border);border-radius:4px;${c.already_imported ? "opacity:.55" : ""}">
                  <strong style="font-family:var(--xmc-font-mono);font-size:.8rem">${c.skill_id}</strong>
                  <span class="xmc-h-badge xmc-h-badge--muted" style="font-size:.6rem">${c.source_label}</span>
                  ${c.already_imported
                    ? html`<span class="xmc-h-badge xmc-h-badge--success" style="font-size:.6rem">已导入</span>`
                    : null}
                  <small style="flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.7;font-size:.7rem">${c.description || c.source_path}</small>
                  ${!c.already_imported
                    ? html`<button
                        class="xmc-h-btn"
                        style="padding:.15rem .55rem;font-size:.72rem"
                        disabled=${busy === c.source_path}
                        onClick=${() => onImport(c.source_path)}
                      >${busy === c.source_path ? "导入中…" : "导入"}</button>`
                    : null}
                </li>
              `)}
              ${importable.length > 1
                ? html`<li style="padding:.3rem .55rem;font-size:.7rem;opacity:.7">
                    💡 导入后 SKILL.md 文件 copy 到 ${data?.private_root}，下次 agent 即可使用。
                    源文件保留原位（不删），其他 agent 可继续读。
                  </li>`
                : null}
            </ul>`}
    </details>
  `;
}

function PanelItem({ icon, label, active, onClick, count }) {
  return html`
    <button
      type="button"
      class=${"xmc-h-skills__panelitem " + (active ? "is-active" : "")}
      onClick=${onClick}
    >
      <${Icon} d=${icon} className="xmc-h-skills__panelitem-icon" />
      <span class="xmc-h-skills__panelitem-label">${label}</span>
      ${count != null
        ? html`<span class="xmc-h-skills__panelitem-count">${count}</span>`
        : null}
    </button>
  `;
}

// ── SkillCard — one skill in the content list ────────────────────

function SkillCard({ skill, expanded, onToggle, onPromote, onRollback }) {
  const sourceTone =
    skill.source === "built-in" ? "success"
    : skill.source === "user" ? "warning"
    : "muted";
  return html`
    <div class="xmc-h-skill-card">
      <button
        type="button"
        class="xmc-h-skill-card__head"
        onClick=${onToggle}
        aria-expanded=${expanded ? "true" : "false"}
      >
        <code class="xmc-h-skill-card__id">${skill.id}</code>
        <span class=${"xmc-h-badge xmc-h-badge--" + sourceTone}>${skill.source}</span>
        <span class="xmc-h-skill-card__head-meta">
          ${skill.versions.length} 个版本 · HEAD = v${skill.head_version}
        </span>
      </button>
      ${expanded
        ? html`
          <div class="xmc-h-skill-card__body">
            <ul class="xmc-h-skill-card__verlist">
              ${skill.versions.map((v) => html`
                <li class=${"xmc-h-skill-card__ver " + (v.is_head ? "is-head" : "")} key=${v.version}>
                  <span class="xmc-h-skill-card__verlabel">v${v.version}</span>
                  ${v.is_head ? html`<span class="xmc-h-badge xmc-h-badge--success">HEAD</span>` : null}
                  <span class="xmc-h-skill-card__verdesc">
                    ${v.manifest?.description || v.manifest?.summary || "—"}
                  </span>
                  <!-- B-115: per-version promote / rollback. HEAD itself is
                       neither (clicking HEAD = no-op). Promote when v >
                       head_version (forward step); rollback when v <. -->
                  ${!v.is_head && v.version > skill.head_version ? html`
                    <button
                      type="button"
                      class="xmc-h-btn xmc-h-btn--primary"
                      style="font-size:.7rem;padding:.15rem .5rem"
                      onClick=${(e) => { e.stopPropagation(); onPromote(skill, v.version); }}
                    >推到此版本</button>
                  ` : null}
                  ${!v.is_head && v.version < skill.head_version ? html`
                    <button
                      type="button"
                      class="xmc-h-btn xmc-h-btn--ghost"
                      style="font-size:.7rem;padding:.15rem .5rem"
                      onClick=${(e) => { e.stopPropagation(); onRollback(skill, v.version); }}
                    >回滚到此版本</button>
                  ` : null}
                </li>
              `)}
            </ul>
          </div>
        `
        : null}
    </div>
  `;
}

// ── SkillsPage main ──────────────────────────────────────────────

export function SkillsPage({ token }) {
  const [skills, setSkills] = useState(null);
  // B-150: also pull SKILL.md learned skills (auto-evo + skills.sh +
  // Claude Code) so the user sees ALL their installed skills here.
  // Pre-B-150 this page only showed Python Skill subclasses, leaving
  // SKILL.md skills hidden under the Evolution page.
  const [learned, setLearned] = useState(null);
  const [scannedRoots, setScannedRoots] = useState([]);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");
  // view: "all" | "built-in" | "user" | "learned"
  const [view, setView] = useState("all");
  const [expanded, setExpanded] = useState(new Set());

  useEffect(() => {
    let cancelled = false;
    apiGet("/api/v2/skills", token)
      .then((d) => { if (!cancelled) setSkills(d.skills || []); })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    apiGet("/api/v2/auto_evo/learned_skills?include_disabled=1", token)
      .then((d) => {
        if (cancelled) return;
        setLearned(d.skills || []);
        setScannedRoots(d.scanned_roots || []);
      })
      .catch(() => { if (!cancelled) setLearned([]); });
    return () => { cancelled = true; };
  }, [token]);

  // B-150: classify each learned skill by its disk path so the user
  // can see WHERE every skill came from (auto-evo / skills.sh /
  // Claude Code / project-local). Helps answer "我都安装了哪些技能".
  const classifyLearnedOrigin = (sk) => {
    const p = (sk.source_path || sk.path || "").replace(/\\/g, "/").toLowerCase();
    if (p.includes("/.xmclaw/auto_evo/skills/")) return { kind: "auto-evo", label: "🤖 自动学的" };
    if (p.includes("/.agents/skills/")) return { kind: "skills.sh", label: "🌐 skills.sh" };
    if (p.includes("/.claude/skills/")) return { kind: "claude-code", label: "🪶 Claude Code" };
    return { kind: "other", label: "📁 其他" };
  };

  const filteredRegistry = useMemo(() => {
    if (!skills) return [];
    const q = search.trim().toLowerCase();
    return skills.filter((s) => {
      if (view === "learned") return false;
      if (view !== "all" && s.source !== view) return false;
      if (!q) return true;
      if (s.id.toLowerCase().includes(q)) return true;
      return (s.versions || []).some((v) => {
        const desc = (v.manifest?.description || "").toLowerCase();
        const summary = (v.manifest?.summary || "").toLowerCase();
        return desc.includes(q) || summary.includes(q);
      });
    });
  }, [skills, search, view]);

  const filteredLearned = useMemo(() => {
    if (!learned) return [];
    if (view !== "all" && view !== "learned") return [];
    const q = search.trim().toLowerCase();
    return learned.filter((s) => {
      if (!q) return true;
      return (
        (s.skill_id || "").toLowerCase().includes(q) ||
        (s.title || "").toLowerCase().includes(q) ||
        (s.description || "").toLowerCase().includes(q)
      );
    });
  }, [learned, search, view]);

  const counts = useMemo(() => {
    const base = { all: 0, "built-in": 0, user: 0, learned: 0 };
    for (const s of skills || []) {
      base.all++;
      base[s.source] = (base[s.source] || 0) + 1;
    }
    for (const _ of learned || []) {
      base.all++;
      base.learned++;
    }
    return base;
  }, [skills, learned]);

  const onToggle = (sid) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(sid)) next.delete(sid); else next.add(sid);
      return next;
    });
  };

  const reload = () => {
    apiGet("/api/v2/skills", token)
      .then((d) => setSkills(d.skills || []))
      .catch((e) => toast.error("刷新失败：" + (e.message || e)));
  };

  // B-161: full reload covers both registry + learned skills + scanned roots.
  const reloadAll = () => {
    reload();
    apiGet("/api/v2/auto_evo/learned_skills?include_disabled=1", token)
      .then((d) => {
        setLearned(d.skills || []);
        setScannedRoots(d.scanned_roots || []);
      })
      .catch(() => {});
  };

  // B-115: manual promote — anti-req #12 requires non-empty evidence,
  // so we prompt the user via a textarea-in-confirm (keeps the existing
  // dialog API minimal). Empty input cancels.
  const onPromote = async (skill, toVersion) => {
    const evidence = window.prompt(
      `推 ${skill.id} 到 v${toVersion}\n\n输入 evidence（必填，至少一行 — 例如 'bench:phase1 +1.12x'）：`,
    );
    if (!evidence || !evidence.trim()) return;
    try {
      const r = await apiPost(
        `/api/v2/skills/${encodeURIComponent(skill.id)}/promote`,
        { to_version: toVersion, evidence: [evidence.trim()] },
        token,
      );
      toast.success(`已推到 v${r.head_version}`);
      reload();
    } catch (e) {
      toast.error("推送失败：" + (e.message || e));
    }
  };

  const onRollback = async (skill, toVersion) => {
    const reason = window.prompt(
      `把 ${skill.id} 从 v${skill.head_version} 回滚到 v${toVersion}\n\n输入回滚原因（必填）：`,
    );
    if (!reason || !reason.trim()) return;
    const ok = await confirmDialog({
      title: `确认回滚 ${skill.id}`,
      body: `从 v${skill.head_version} → v${toVersion}\n\n原因：${reason.trim()}`,
      confirmLabel: "回滚",
      confirmTone: "danger",
    });
    if (!ok) return;
    try {
      const r = await apiPost(
        `/api/v2/skills/${encodeURIComponent(skill.id)}/rollback`,
        { to_version: toVersion, reason: reason.trim() },
        token,
      );
      toast.success(`已回滚到 v${r.head_version}`);
      reload();
    } catch (e) {
      toast.error("回滚失败：" + (e.message || e));
    }
  };

  if (error) {
    return html`
      <section class="xmc-h-page" aria-labelledby="skills-title">
        <header class="xmc-h-page__header">
          <h2 id="skills-title" class="xmc-h-page__title">技能</h2>
        </header>
        <div class="xmc-h-page__body">
          <div class="xmc-h-error">${error}</div>
        </div>
      </section>
    `;
  }

  if (skills === null) {
    return html`
      <section class="xmc-h-page" aria-labelledby="skills-title">
        <header class="xmc-h-page__header">
          <h2 id="skills-title" class="xmc-h-page__title">技能</h2>
        </header>
        <div class="xmc-h-page__body">
          <div class="xmc-h-loading">载入中…</div>
        </div>
      </section>
    `;
  }

  return html`
    <section class="xmc-h-page" aria-labelledby="skills-title">
      <header class="xmc-h-page__header">
        <div class="xmc-h-page__heading">
          <h2 id="skills-title" class="xmc-h-page__title">技能</h2>
          <p class="xmc-h-page__subtitle">
            统一视图：你装的所有技能 + agent 自学的技能。
            <strong>${counts.all}</strong> 个总计 ·
            <strong>${counts.learned}</strong> 个 SKILL.md ·
            <strong>${counts["built-in"] + counts.user}</strong> 个 Python Skill。
          </p>
        </div>
        <div class="xmc-h-page__actions">
          <span class="xmc-h-badge">${counts.all} 个</span>
        </div>
      </header>

      ${scannedRoots.length ? html`
        <details style="margin:.5rem 0;padding:.5rem .7rem;border:1px solid var(--color-border);border-radius:6px;background:color-mix(in srgb, var(--midground) 4%, transparent)">
          <summary style="cursor:pointer;font-size:.85rem">
            <strong>📂 扫描路径</strong> <small style="opacity:.7">(B-161 — XMclaw 默认只扫私有目录，外部 skills 显式导入)</small>
          </summary>
          <ul style="list-style:none;padding:.4rem 0 0;margin:0;font-size:.78rem">
            ${scannedRoots.map((r) => html`
              <li key=${r.path} style="display:flex;gap:.5rem;align-items:center;padding:.15rem 0">
                ${r.exists
                  ? html`<span class="xmc-h-badge xmc-h-badge--success">✓ 存在</span>`
                  : html`<span class="xmc-h-badge xmc-h-badge--muted">未创建</span>`}
                <code style="font-size:.72rem">${r.path}</code>
              </li>
            `)}
          </ul>
          <p style="margin:.5rem 0 0;font-size:.72rem;opacity:.75;line-height:1.5">
            <strong>B-161 隔离策略：</strong>不再自动扫描 <code>~/.agents/skills/</code> /
            <code>~/.claude/skills/</code>（其他 agent 共享路径），避免供应链攻击 + 跨 agent 污染。
            想用那边的技能 → 下方"导入外部技能"显式 copy 一份到私有目录。
          </p>
        </details>
        <${ImportExternalSkills} token=${token} onImported=${reloadAll} />
      ` : null}

      <div class="xmc-h-page__body xmc-h-skills__body">
        <aside class="xmc-h-skills__panel" aria-label="过滤">
          <div class="xmc-h-skills__panel-head">
            <${Icon} d=${I_FILTER} className="xmc-h-skills__panel-icon" />
            <span>过滤</span>
          </div>
          <div class="xmc-h-skills__panel-list">
            <${PanelItem}
              icon=${I_PACKAGE}
              label="全部"
              count=${counts.all}
              active=${view === "all"}
              onClick=${() => setView("all")}
            />
            <${PanelItem}
              icon=${I_PACKAGE}
              label="内置"
              count=${counts["built-in"] || 0}
              active=${view === "built-in"}
              onClick=${() => setView("built-in")}
            />
            <${PanelItem}
              icon=${I_SPARKLES}
              label="用户 (Python)"
              count=${counts.user || 0}
              active=${view === "user"}
              onClick=${() => setView("user")}
            />
            <${PanelItem}
              icon=${I_SPARKLES}
              label="已学 (SKILL.md)"
              count=${counts.learned || 0}
              active=${view === "learned"}
              onClick=${() => setView("learned")}
            />
          </div>
        </aside>

        <div class="xmc-h-skills__content">
          <div class="xmc-h-skills__searchbar">
            <span class="xmc-h-skills__searchicon">
              <${Icon} d=${I_SEARCH} />
            </span>
            <input
              type="search"
              class="xmc-h-input"
              placeholder="搜索技能 / 描述…"
              value=${search}
              onInput=${(e) => setSearch(e.target.value)}
            />
            ${search
              ? html`
                <button
                  type="button"
                  class="xmc-h-skills__searchclear"
                  onClick=${() => setSearch("")}
                  aria-label="clear"
                ><${Icon} d=${I_X} /></button>
              `
              : null}
          </div>

          ${(filteredRegistry.length + filteredLearned.length) === 0
            ? html`<div class="xmc-h-empty">${
                search ? "没有匹配的技能。" :
                view !== "all" ? `这个分类下还没有技能。` :
                html`<div style="line-height:1.7">
                  <p style="margin:0 0 .5rem"><strong>还没有任何技能。</strong></p>
                  <p style="margin:0;font-size:.85rem">三种方式安装：</p>
                  <ul style="font-size:.8rem;margin:.3rem 0 0;line-height:1.6">
                    <li><code>npx skills add &lt;url&gt; --skill &lt;name&gt;</code> → 装到 <code>~/.agents/skills/</code> (skills.sh 标准)</li>
                    <li>丢 <code>SKILL.md</code> 到 <code>~/.xmclaw/auto_evo/skills/&lt;name&gt;/</code> (auto-evo 路径)</li>
                    <li>写 Python 类丢 <code>~/.xmclaw/skills_user/&lt;id&gt;/skill.py</code> (B-127 SkillRegistry)</li>
                  </ul>
                  <p style="margin:.5rem 0 0;font-size:.78rem;opacity:.7">改完 <strong>重启 daemon</strong> 后页面刷新即可看到。</p>
                </div>`
              }</div>`
            : html`
              <div class="xmc-h-skill-card__list">
                ${filteredRegistry.map((s) => html`
                  <${SkillCard}
                    key=${s.id}
                    skill=${s}
                    expanded=${expanded.has(s.id)}
                    onToggle=${() => onToggle(s.id)}
                    onPromote=${onPromote}
                    onRollback=${onRollback}
                  />
                `)}
                ${filteredLearned.map((sk) => {
                  const origin = classifyLearnedOrigin(sk);
                  const writes30 = sk.invocation_count_30d || 0;
                  const writesAll = sk.invocation_count || 0;
                  const usable = !sk.disabled;
                  // B-158: same base_id 多版本时，标题用 base_id，
                  // 显示 v<N> + 旧版本数 badge
                  const olderCount = (sk.older_versions || []).length;
                  const displayId = sk.base_id && sk.version != null ? sk.base_id : sk.skill_id;
                  return html`
                    <div class="xmc-h-skill-card" key=${"L-" + sk.skill_id}>
                      <div class="xmc-h-skill-card__head" style="cursor:default;display:flex;align-items:baseline;gap:.5rem;flex-wrap:wrap">
                        <code class="xmc-h-skill-card__id">${displayId}</code>
                        ${sk.version != null
                          ? html`<span class="xmc-h-badge xmc-h-badge--info" title=${`当前最新版本 v${sk.version}${olderCount > 0 ? `；磁盘上还有 ${olderCount} 个旧版本被去重 (B-158)` : ""}`}>v${sk.version}${olderCount > 0 ? ` · +${olderCount} 旧版` : ""}</span>`
                          : null}
                        <span class="xmc-h-badge xmc-h-badge--muted">SKILL.md</span>
                        <span class="xmc-h-badge xmc-h-badge--info" title="技能来源 (B-149)">${origin.label}</span>
                        ${usable
                          ? html`<span class="xmc-h-badge xmc-h-badge--success" title="agent 可调用">✓ 可用</span>`
                          : html`<span class="xmc-h-badge xmc-h-badge--warn" title="frontmatter disabled:true">⏸ 暂停</span>`}
                        ${writesAll > 0
                          ? html`<span class="xmc-h-badge xmc-h-badge--success">⚡ ${writesAll} 调用</span>`
                          : html`<span class="xmc-h-badge xmc-h-badge--muted">0 调用</span>`}
                        ${writes30 > 0 && writes30 !== writesAll ? html`<small style="opacity:.7">(30d: ${writes30})</small>` : null}
                      </div>
                      <div class="xmc-h-skill-card__body" style="padding:.4rem .8rem .6rem">
                        ${sk.title && sk.title !== sk.skill_id
                          ? html`<div style="font-size:.85rem;margin-bottom:.2rem"><strong>${sk.title}</strong></div>`
                          : null}
                        ${sk.description
                          ? html`<small style="display:block;color:var(--xmc-fg-muted);margin-bottom:.3rem">${sk.description.slice(0, 200)}</small>`
                          : null}
                        <small style="display:block;font-size:.7rem;opacity:.6;font-family:var(--xmc-font-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title=${sk.source_path || ""}>
                          📁 ${sk.source_path || "(unknown)"}
                        </small>
                        ${(sk.triggers || []).length
                          ? html`<div style="margin-top:.3rem;display:flex;gap:.3rem;flex-wrap:wrap">
                              ${sk.triggers.slice(0, 5).map((t) => html`<code style="font-size:.65rem;background:color-mix(in srgb, var(--color-primary) 10%, transparent);padding:1px 5px;border-radius:3px" key=${t}>${t}</code>`)}
                            </div>`
                          : null}
                      </div>
                    </div>
                  `;
                })}
              </div>
            `}
        </div>
      </div>
    </section>
  `;
}
