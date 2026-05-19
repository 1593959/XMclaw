// XMclaw — SkillsPage (Epic #24 Phase 1 simplified).
//
// Single source of truth: GET /api/v2/skills returns
//   {skills: [{id, head_version, source, versions: [{version, is_head, manifest}]}],
//    evolution_enabled}
//
// All skills go through SkillRegistry — built-in (xmclaw.skills.*) and
// user-installed are the only two sources. The user-loader scans two
// roots by default (zero config): the canonical
// `~/.xmclaw/skills_user/` and `~/.agents/skills/` (the open
// agent-skills marketplace; ``npx skills add ...`` writes here). The
// old xm-auto-evo path was torn out in Phase 1; SkillProposer /
// SkillDream candidates flow through `SKILL_DRAFTED` bus events →
// ProposalMaterializer (B-167) writes SKILL.md to disk +
// SkillRegistry.register(set_head=True), so this page stays the one
// place the user goes for "what skills does my agent have?".
// (B-333 fix: comment used to reference a non-existent
// `SkillRegistry.add_candidate` method — the real wiring is event +
// register, not a method on the registry. B-341 (audit pass-2 #9)
// fix: removed the ``~/.claude/skills`` mention; B-234 dropped it
// from the default — Claude Code's user-level config space is not
// XMclaw's territory. Users who want to share between the two opt
// in via ``config.evolution.skill_paths.extra``.)
//
// Layout: sticky left filter panel (All / Built-in / User) + content
// area with version-ladder cards. Promote / rollback land manual
// edits through `/api/v2/skills/<id>/{promote,rollback}` — both routes
// enforce anti-req #12 (evidence required) at the registry door.

const { h } = window.__xmc.preact;
const { useState, useEffect, useMemo } = window.__xmc.preact_hooks;
const html = window.__xmc.htm.bind(h);

import { apiGet, apiPost } from "../lib/api.js";
import { confirmDialog } from "../lib/dialog.js";
import { toast } from "../lib/toast.js";
import { Skeleton } from "../components/atoms/skeleton.js";

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

// B-166: source values now include "user" / "evolved" / "llm" /
// "built-in" / "unknown". Map each to a distinct badge tone + label
// so the user can see at a glance what produced each skill.
const SOURCE_META = {
  "built-in": { tone: "success", label: "BUILT-IN" },
  "user":     { tone: "warning", label: "USER" },
  "evolved":  { tone: "info",    label: "EVOLVED" },
  "llm":      { tone: "info",    label: "LLM-DRAFT" },
  "unknown":  { tone: "muted",   label: "UNKNOWN" },
};

function SkillCard({ skill, expanded, onToggle, onPromote, onRollback }) {
  const meta = SOURCE_META[skill.source] || SOURCE_META.unknown;
  return html`
    <div class="xmc-h-skill-card">
      <button
        type="button"
        class="xmc-h-skill-card__head"
        onClick=${onToggle}
        aria-expanded=${expanded ? "true" : "false"}
      >
        <code class="xmc-h-skill-card__id">${skill.id}</code>
        <span class=${"xmc-h-badge xmc-h-badge--" + meta.tone}>${meta.label}</span>
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

export function SkillsPage({ token }) {
  const [skills, setSkills] = useState(null);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState("");
  // view: "all" | "built-in" | "user"
  const [view, setView] = useState("all");
  const [expanded, setExpanded] = useState(new Set());
  // B-341 (audit pass-2 #6): when the daemon's SkillsWatcher detects
  // an mtime change on a registered Python ``skill.py`` it can't
  // hot-reload (importlib cache) — it emits SKILL_UPDATE_REQUIRES_
  // RESTART on the bus AND now (post-B-341) populates
  // ``/api/v2/skills.pending_restarts``. Pre-B-341 the bus event
  // had zero subscribers; this banner is the missing consumer.
  const [pendingRestarts, setPendingRestarts] = useState([]);
  // Epic #27 P0 G-02 (2026-05-19): UserSkillsLoader failures land
  // here via SkillsWatcher.load_failures(). Pre-G-02 the data only
  // existed in daemon.log — the agent/user had no way to see "your
  // skill.py couldn't instantiate, that's why skill_browse can't
  // find it". The banner below renders the actionable subset.
  const [loadFailures, setLoadFailures] = useState([]);

  useEffect(() => {
    let cancelled = false;
    apiGet("/api/v2/skills", token)
      .then((d) => {
        if (cancelled) return;
        setSkills(d.skills || []);
        setPendingRestarts(d.pending_restarts || []);
        setLoadFailures(d.load_failures || []);
      })
      .catch((e) => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [token]);

  const filtered = useMemo(() => {
    if (!skills) return [];
    const q = search.trim().toLowerCase();
    return skills.filter((s) => {
      // B-166: "user" filter folds evolved/llm/user — they're all
      // "things XMclaw didn't ship with". Built-in stays its own
      // bucket.
      if (view === "built-in" && s.source !== "built-in") return false;
      if (view === "user" && s.source === "built-in") return false;
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
    // B-166: "user" lane counts everything that wasn't shipped with
    // XMclaw — manually-installed (created_by=user), evolution-promoted
    // (evolved), and LLM-drafted (llm).
    const base = { all: 0, "built-in": 0, user: 0, evolved: 0, llm: 0 };
    for (const s of skills || []) {
      base.all++;
      if (s.source === "built-in") {
        base["built-in"]++;
      } else {
        base.user++;
        if (s.source === "evolved") base.evolved++;
        if (s.source === "llm") base.llm++;
      }
    }
    return base;
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
      .then((d) => {
        setSkills(d.skills || []);
        setPendingRestarts(d.pending_restarts || []);
        setLoadFailures(d.load_failures || []);
      })
      .catch((e) => toast.error("刷新失败：" + (e.message || e)));
  };

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
          <div style="padding:1rem"><${Skeleton} lines=${4} /></div>
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
            统一视图：所有 SkillRegistry 注册的技能 ·
            <strong>${counts.all}</strong> 个 ·
            <strong>${counts["built-in"]}</strong> 个内置 ·
            <strong>${counts.user}</strong> 个用户/进化产出
            ${counts.evolved
              ? html`<small style="opacity:.65">（其中 ${counts.evolved} 进化）</small>`
              : null}。
          </p>
        </div>
        <div class="xmc-h-page__actions">
          <span class="xmc-h-badge">${counts.all} 个</span>
        </div>
      </header>

      ${loadFailures.length > 0
        ? html`
            <div
              class="xmc-h-banner xmc-h-banner--error"
              role="alert"
              aria-live="polite"
              style=${"margin: 8px 16px; padding: 10px 14px; "
                + "border-radius: 6px; "
                + "background: rgba(239, 68, 68, 0.14); "
                + "border: 1px solid rgba(239, 68, 68, 0.55); "
                + "color: var(--xmc-text, inherit);"}
            >
              <strong>${loadFailures.length} 个技能加载失败</strong>
              <span style="opacity:.8">
                · 这些目录被 SkillsWatcher 看到，但实例化时报错。
                修完 ``skill.py`` 重启 daemon 即可消除；或 file_read
                <code>${loadFailures[0].path}</code> 看具体错误。
              </span>
              <ul style="margin: 6px 0 0 18px; padding: 0; opacity: .9">
                ${loadFailures.map(
                  (it) => html`
                    <li style="font-family: var(--xmc-mono, monospace); font-size: .85em">
                      <strong>${it.skill_id}</strong>
                      <span style="opacity:.7"> (${it.kind}) </span>
                      <span style="opacity:.8">— ${it.error}</span>
                      ${it.ticks_failing > 1
                        ? html`<span style="opacity:.55"> · 连续 ${it.ticks_failing} 个 tick 失败</span>`
                        : null}
                    </li>
                  `,
                )}
              </ul>
            </div>
          `
        : null}

      ${pendingRestarts.length > 0
        ? html`
            <div
              class="xmc-h-banner xmc-h-banner--warning"
              role="status"
              aria-live="polite"
              style=${"margin: 8px 16px; padding: 10px 14px; "
                + "border-radius: 6px; "
                + "background: rgba(245, 158, 11, 0.12); "
                + "border: 1px solid rgba(245, 158, 11, 0.5); "
                + "color: var(--xmc-text, inherit);"}
            >
              <strong>
                ${pendingRestarts.some((r) => r.state === "fixed_after_failure")
                  ? "看起来你修好了某个坏 skill — 需要重启 daemon 才会加载新版"
                  : "需要重启 daemon 才能生效"}
              </strong>
              <span style="opacity:.8">
                · 检测到 ${pendingRestarts.length} 个 Python 技能
                文件被编辑：
              </span>
              <ul style="margin: 6px 0 0 18px; padding: 0; opacity: .85">
                ${pendingRestarts.map(
                  (it) => html`
                    <li style="font-family: var(--xmc-mono, monospace); font-size: .85em">
                      ${it.skill_id} v${it.version}
                      ${it.state === "fixed_after_failure"
                        ? html`<span style="color:#10b981"> ✓ fixed</span>`
                        : null}
                      <span style="opacity: .6"> — ${it.path}</span>
                    </li>
                  `,
                )}
              </ul>
              <div style="margin-top: 6px; opacity:.7; font-size:.85em">
                Python skill 由 importlib 缓存 — 运行
                <code>xmclaw stop &amp;&amp; xmclaw start</code> 后才会
                加载新代码（SKILL.md 编辑会自动热更新，无需重启）。
              </div>
            </div>
          `
        : null}

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
                html`<div style="line-height:1.7">
                  <p style="margin:0 0 .5rem"><strong>还没有任何技能。</strong></p>
                  <p style="margin:0 0 .5rem;font-size:.85rem">
                    daemon 自动扫这两个目录，谁先匹配 skill_id 谁先入库——
                    <strong>零 config，~10s 内即生效（B-173 起无需重启）</strong>：
                  </p>
                  <ul style="margin:.2rem 0;padding-left:1.2rem;font-size:.82rem">
                    <li><code>~/.xmclaw/skills_user/&lt;skill_id&gt;/</code> ← 规范路径（首选）</li>
                    <li><code>~/.agents/skills/&lt;skill_id&gt;/</code> ← <code>npx skills add</code> 默认</li>
                  </ul>
                  <p style="margin:.4rem 0 .2rem;font-size:.85rem">
                    每个目录里 <code>skill.py</code>（Python 子类）或 <code>SKILL.md</code>（Markdown 步骤）二选一即可。
                  </p>
                  <p style="margin:.4rem 0 0;font-size:.78rem;opacity:.75">
                    想关共享扫描？<code>daemon/config.json</code> 加 <code>"evolution":{"skill_paths":{"extra":[]}}</code>。<br/>
                    想加自定义路径（含 <code>~/.claude/skills</code>）？同字段写绝对路径列表。
                  </p>
                  <p style="margin:.6rem 0 0">
                    <a href="/ui/marketplace" style="display:inline-block;padding:.4rem .8rem;background:var(--xmc-accent);color:var(--xmc-accent-fg);border-radius:6px;text-decoration:none;font-size:.82rem">去技能商店看看 →</a>
                  </p>
                </div>`
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
