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
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");
  const [view, setView] = useState("all"); // "all" | "built-in" | "user"
  const [expanded, setExpanded] = useState(new Set());

  useEffect(() => {
    let cancelled = false;
    apiGet("/api/v2/skills", token)
      .then((d) => { if (!cancelled) setSkills(d.skills || []); })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [token]);

  const filtered = useMemo(() => {
    if (!skills) return [];
    const q = search.trim().toLowerCase();
    return skills.filter((s) => {
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

  const counts = useMemo(() => {
    if (!skills) return { all: 0, "built-in": 0, user: 0 };
    return skills.reduce(
      (acc, s) => {
        acc.all++;
        acc[s.source] = (acc[s.source] || 0) + 1;
        return acc;
      },
      { all: 0, "built-in": 0, user: 0 },
    );
  }, [skills]);

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
            内置 + 用户安装技能（来自 SkillRegistry）。HEAD 版本随
            EvolutionController 的 promotion gate 自动切换。
          </p>
        </div>
        <div class="xmc-h-page__actions">
          <span class="xmc-h-badge">${counts.all} 个 / ${
            (skills || []).reduce((a, s) => a + s.versions.length, 0)
          } 版本</span>
        </div>
      </header>

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
              label="用户"
              count=${counts.user || 0}
              active=${view === "user"}
              onClick=${() => setView("user")}
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

          ${filtered.length === 0
            ? html`<div class="xmc-h-empty">${
                search ? "没有匹配的技能。" :
                view !== "all" ? `这个分类下还没有技能。` :
                "SkillRegistry 还没注册技能。"
              }</div>`
            : html`
              <div class="xmc-h-skill-card__list">
                ${filtered.map((s) => html`
                  <${SkillCard}
                    key=${s.id}
                    skill=${s}
                    expanded=${expanded.has(s.id)}
                    onToggle=${() => onToggle(s.id)}
                    onPromote=${onPromote}
                    onRollback=${onRollback}
                  />
                `)}
              </div>
            `}
        </div>
      </div>
    </section>
  `;
}
