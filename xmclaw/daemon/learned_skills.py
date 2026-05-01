"""LearnedSkillsLoader — closes the evolution loop.

xm-auto-evo writes SKILL.md files under
``~/.xmclaw/auto_evo/skills/auto_<name>/SKILL.md`` when its
``auto_skill_creation`` path fires. Without a consumer those files
just sit on disk — the agent doesn't know they exist, the user gets
"the system evolved!" with no behaviour change. That's exactly the
"光进化无法用" trap the user called out in B-17.

This loader fixes that. On every system-prompt rebuild it scans the
auto_evo skills directory and produces a markdown block that's
appended to the prompt:

    ## 已学习的技能（自动进化产物）

    ### auto_xxxxx
    <SKILL.md frontmatter description + first paragraph>

    Trigger: <signals_match>
    How to use: <body excerpt>
    ...

The agent reads this on every turn. When it encounters a matching
pattern, it follows the SKILL.md procedure using its existing tools
(file_read / bash / etc) — no new tool registration required, no
sandboxed code execution. The skill is "code as instructions for
the agent", which is what xm-auto-evo's autoCreateSkill produces
anyway.

When the agent successfully completes a learned-skill procedure,
DialogExporter records the activity → next xm-auto-evo observe
cycle sees the reinforcement → gene's v_score goes up → loop
closed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


_VERSION_SUFFIX_RE = re.compile(r"^(?P<base>.+)_v(?P<n>\d+)$")


def _split_versioned(skill_id: str) -> tuple[str, int | None]:
    """B-158: split ``auto_repair_bdf153_v38`` → ('auto_repair_bdf153', 38).

    Returns ``(skill_id, None)`` when the id doesn't end in ``_v<N>``.
    Used by both the loader (dedup-by-base) and list_for_api (so the
    UI can group versions of the same conceptual skill).
    """
    m = _VERSION_SUFFIX_RE.match(skill_id or "")
    if m is None:
        return skill_id, None
    return m.group("base"), int(m.group("n"))


def _dedupe_versioned(skills: "list[LearnedSkill]") -> "list[LearnedSkill]":
    """B-158: keep only the highest ``_v<N>`` per base_id.

    Skills without a ``_v<N>`` suffix pass through untouched (single-
    version SKILL.md from the user / skills.sh). The auto-evo writer
    appends ``_v<N>`` on every iteration so this drops stale versions
    that would otherwise pollute the agent's prompt with overlapping
    procedures.
    """
    # Group by base_id.
    by_base: dict[str, list[tuple[int, LearnedSkill]]] = {}
    no_version: list[LearnedSkill] = []
    for sk in skills:
        base, n = _split_versioned(sk.skill_id)
        if n is None:
            no_version.append(sk)
        else:
            by_base.setdefault(base, []).append((n, sk))
    out: list[LearnedSkill] = list(no_version)
    for base, entries in by_base.items():
        entries.sort(key=lambda t: t[0], reverse=True)
        # Keep only the latest. The rest are dropped from runtime view
        # (still on disk for audit / rollback).
        out.append(entries[0][1])
    return out


@dataclass(frozen=True, slots=True)
class LearnedSkill:
    """One skill loaded from a SKILL.md file."""

    skill_id: str           # directory stem, e.g. "auto_xxxxx"
    title: str              # first '#' heading in the body
    description: str        # frontmatter `description`, fallback to first paragraph
    triggers: list[str]     # frontmatter `signals_match` or derived from description
    body: str               # the full SKILL.md (sans frontmatter) — first 1500 chars
    source_path: Path
    mtime: float            # for cache invalidation


_FRONTMATTER_RE = re.compile(
    r"\A---\n(.*?)\n---\n(.*)", re.DOTALL,
)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Lightweight YAML-front-matter parser. Doesn't depend on PyYAML
    for the same reason ``utils/paths.py`` doesn't — keeps boot fast.
    Handles the common shapes that xm-auto-evo's skill_maker emits."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw_fm = m.group(1)
    body = m.group(2)
    fm: dict[str, object] = {}
    current_key: str | None = None
    current_list: list[str] | None = None
    for line in raw_fm.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - ") and current_list is not None:
            current_list.append(line[4:].strip().strip('"').strip("'"))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            current_list = []
            fm[key] = current_list
            current_key = key
            continue
        # Inline value
        current_list = None
        current_key = key
        fm[key] = value.strip('"').strip("'")
    return fm, body


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s.lstrip("# ").strip()
    return ""


def _first_paragraph(body: str, *, max_chars: int = 240) -> str:
    paras: list[str] = []
    cur: list[str] = []
    for line in body.splitlines():
        if line.strip():
            cur.append(line.rstrip())
        elif cur:
            paras.append(" ".join(cur))
            cur = []
            if paras:
                break
    if cur and not paras:
        paras.append(" ".join(cur))
    if not paras:
        return ""
    return paras[0][:max_chars]


def _load_one(
    skill_dir: Path,
    *,
    inline_shell_enabled: bool = False,
    template_ctx: dict | None = None,
) -> LearnedSkill | None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        stat = skill_md.stat()
    except OSError:
        return None
    fm, body = _parse_frontmatter(text)

    # B-33: ``disabled: true`` (or ``enabled: false``) frontmatter
    # opt-out. Lets the user park a misfiring skill without deleting
    # the file. Recognised values are case-insensitive truthy strings.
    def _truthy(v: object) -> bool:
        return str(v).strip().lower() in ("true", "yes", "1", "on")

    if _truthy(fm.get("disabled")):
        return None
    enabled_val = fm.get("enabled")
    if enabled_val is not None and not _truthy(enabled_val):
        return None

    # B-24 (Hermes parity): expand template variables and (optionally)
    # inline shell snippets in the body BEFORE we hand it to the agent.
    # Frontmatter is left raw — it's structural metadata, not prose.
    try:
        from xmclaw.daemon.skill_template import (
            substitute_template_vars,
            expand_inline_shell,
        )
        ctx = dict(template_ctx or {})
        ctx.setdefault("skill_dir", skill_dir)
        body = substitute_template_vars(body, **ctx)
        if inline_shell_enabled:
            body = expand_inline_shell(body, cwd=skill_dir)
    except Exception:  # noqa: BLE001 — never let template expansion
        # break the loader; fall back to raw body
        pass

    # B-24 skill_guard: scan the (post-substitution) body for
    # destructive / injection patterns. xm-auto-evo is autonomous;
    # in principle it could synthesise a SKILL.md that tells the
    # agent to ``rm -rf /`` or curl-pipe-shell. We default to the
    # ``agent-created`` trust tier — caution-warn, dangerous-block.
    # ``builtin``/``trusted`` skills (from frontmatter ``trust`` key)
    # bypass with a higher tolerance.
    skill_action = "allow"
    skill_scan_summary = ""
    try:
        from xmclaw.security.skill_guard import (
            scan_skill_content,
            apply_policy,
            TrustLevel,
        )
        trust_str = str(fm.get("trust") or "").lower().strip()
        try:
            trust_lvl = TrustLevel(trust_str) if trust_str else TrustLevel.AGENT_CREATED
        except ValueError:
            trust_lvl = TrustLevel.AGENT_CREATED
        scan = scan_skill_content(body)
        skill_action, skill_scan_summary = apply_policy(scan, trust=trust_lvl)
        if skill_action == "block":
            from xmclaw.utils.log import get_logger
            get_logger(__name__).warning(
                "skill_guard.blocked skill=%s reason=%s",
                skill_dir.name, skill_scan_summary,
            )
            return None  # Drop the skill — agent never sees it.
        if skill_action == "warn":
            from xmclaw.utils.log import get_logger
            get_logger(__name__).warning(
                "skill_guard.warning skill=%s reason=%s",
                skill_dir.name, skill_scan_summary,
            )
    except Exception:  # noqa: BLE001 — scan failure must not block
        pass
    title = (
        str(fm.get("name") or "")
        or _first_heading(body)
        or skill_dir.name
    )
    description = (
        str(fm.get("description") or "")
        or _first_paragraph(body)
    )
    triggers_raw = fm.get("signals_match") or fm.get("triggers") or []
    triggers: list[str]
    if isinstance(triggers_raw, list):
        triggers = [str(t) for t in triggers_raw if str(t).strip()]
    else:
        triggers = [str(triggers_raw)] if str(triggers_raw).strip() else []
    return LearnedSkill(
        skill_id=skill_dir.name,
        title=title,
        description=description,
        triggers=triggers,
        body=body[:1500],
        source_path=skill_md,
        mtime=stat.st_mtime,
    )


class LearnedSkillsLoader:
    """Reads ~/.xmclaw/auto_evo/skills/* and renders a system-prompt
    section. Cached by (skills_dir mtime, set of skill mtimes) so a
    no-op rebuild is free.

    B-24: inline-shell expansion is config-gated
    (``evolution.auto_evo.inline_shell.enabled`` — default False).
    Template variables (``${XMC_SKILL_DIR}`` etc.) are always on.
    """

    def __init__(
        self,
        skills_root: Path,
        *,
        inline_shell_enabled: bool = False,
        workspace_provider: "object | None" = None,
        profile_dir_provider: "object | None" = None,
        extra_roots: "list[Path] | None" = None,
    ) -> None:
        self._root = skills_root
        # B-149: extra roots — secondary directories to ALSO scan.
        # Lets XMclaw pick up Anthropic Agent Skills installed via
        # ``npx skills add ...`` (lands in ``~/.agents/skills/``) or
        # Claude Code (``~/.claude/skills/``) without forcing the user
        # to copy / symlink. Same SKILL.md format on disk, multiple
        # ecosystems share the standard.
        self._extra_roots: list[Path] = list(extra_roots or [])
        self._inline_shell_enabled = bool(inline_shell_enabled)
        self._workspace_provider = workspace_provider
        self._profile_dir_provider = profile_dir_provider
        self._cache_key: tuple | None = None
        self._cache_block: str = ""
        self._cache_skills: list[LearnedSkill] = []

    def _template_ctx(self, skill_dir: Path) -> dict:
        """Resolve runtime values (workspace path, profile dir) lazily
        per skill load. Providers may return None when nothing's wired
        — that's fine, the substituter leaves the token in place."""
        ctx: dict = {"skill_dir": skill_dir}
        try:
            if self._workspace_provider is not None:
                v = self._workspace_provider()
                if v is not None:
                    ctx["workspace"] = Path(str(v))
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._profile_dir_provider is not None:
                v = self._profile_dir_provider()
                if v is not None:
                    ctx["profile_dir"] = Path(str(v))
        except Exception:  # noqa: BLE001
            pass
        return ctx

    @property
    def skills_root(self) -> Path:
        return self._root

    @property
    def all_roots(self) -> list[Path]:
        """Every directory this loader scans, in order. Primary first,
        then extras (B-149: skills.sh / Claude Code shared paths)."""
        return [self._root, *self._extra_roots]

    def _scan(self) -> list[LearnedSkill]:
        skills: list[LearnedSkill] = []
        seen_ids: set[str] = set()
        for root in self.all_roots:
            if not root.is_dir():
                continue
            try:
                entries = sorted(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir():
                    continue
                # B-149: skill_id-level dedup across roots so the same
                # skill installed in two ecosystems (e.g. ~/.agents/
                # skills/find-skills AND ~/.claude/skills/find-skills)
                # appears once. Primary root wins (ours).
                if entry.name in seen_ids:
                    continue
                sk = _load_one(
                    entry,
                    inline_shell_enabled=self._inline_shell_enabled,
                    template_ctx=self._template_ctx(entry),
                )
                if sk is not None:
                    skills.append(sk)
                    seen_ids.add(entry.name)
        # B-158: dedupe by base_id when skill_id ends with _v<N>.
        # xm-auto-evo writes a fresh dir per iteration (auto_repair_v37
        # → auto_repair_v38 → ...) and prior to B-158 BOTH got loaded,
        # injecting two competing procedures into the agent's prompt.
        # Now: keep only the highest version per base_id, drop the rest.
        # Same machinery for any user that adopts the _v<N> convention.
        return _dedupe_versioned(skills)

    def _fingerprint(self, skills: list[LearnedSkill]) -> tuple:
        return tuple(sorted((s.skill_id, s.mtime) for s in skills))

    def list_skills(self) -> list[LearnedSkill]:
        skills = self._scan()
        # Stable order: most recently modified first so the agent sees
        # newly-learned skills near the top of the prompt section.
        skills.sort(key=lambda s: s.mtime, reverse=True)
        return skills

    def render_section(self, *, max_skills: int = 12) -> str:
        """Return the markdown block to splice into the system prompt.
        Empty string when no skills exist (no need to clutter the prompt)."""
        skills = self.list_skills()
        fp = self._fingerprint(skills)
        if fp == self._cache_key:
            return self._cache_block

        # B-32: skill set changed since last render — bump the global
        # prompt-freeze generation so every session's frozen system
        # prompt invalidates on its next turn. Closes the "real-time
        # cross-session" gap: xm-auto-evo writes a new SKILL.md →
        # next agent turn (in any session) sees it without daemon
        # restart. We deliberately bump only on FIRST detection of a
        # diff (subsequent renders inside the same generation hit the
        # fp-match short-circuit above).
        #
        # Skip on the very first render (cache_key was None) — that's
        # not a "change", it's initial state. Sessions starting after
        # boot pick up the current set anyway.
        if self._cache_key is not None:
            try:
                from xmclaw.daemon.agent_loop import bump_prompt_freeze_generation
                bump_prompt_freeze_generation()
                _log.info(
                    "learned_skills.changed bumping prompt cache "
                    "old_count=%d new_count=%d",
                    len(self._cache_skills), len(skills),
                )
            except Exception:  # noqa: BLE001 — bumping is observability-grade
                pass

        if not skills:
            self._cache_key = fp
            self._cache_block = ""
            self._cache_skills = []
            return ""

        lines: list[str] = []
        lines.append("## 已学习的技能（XMclaw 自主进化产出）")
        lines.append("")
        lines.append(
            "下面是你（XMclaw）通过观察用户对话自主总结出的技能。"
            "每个技能都暴露为一个 `learned_skill_<id>` 工具 — "
            "当用户请求 **匹配下面的 trigger** 时，"
            "**调用对应工具**取回完整步骤，再按步骤执行。"
            "比从零思考更可靠。"
        )
        lines.append("")
        for sk in skills[:max_skills]:
            # B-126: index-only — title + description + triggers + tool name.
            # Full body is no longer pre-injected; the agent retrieves it on
            # demand via the matching learned_skill_<id> tool (B-125). Drops
            # ~600 chars × N skills from the system prompt and turns the
            # heuristic SKILL_INVOKED detection into a deterministic tool-call.
            tool_name = f"learned_skill_{sk.skill_id.replace('.', '__')}"
            lines.append(f"### {sk.title or sk.skill_id}")
            lines.append("")
            if sk.description:
                lines.append(f"_{sk.description}_")
                lines.append("")
            if sk.triggers:
                lines.append(
                    "**触发信号:** "
                    + ", ".join(f"`{t}`" for t in sk.triggers[:6])
                )
                lines.append("")
            lines.append(f"**调用:** `{tool_name}`（无参数；返回完整流程）")
            lines.append("")

        block = "\n".join(lines).rstrip()
        self._cache_key = fp
        self._cache_block = block
        self._cache_skills = skills
        return block

    def list_for_api(self, *, include_disabled: bool = False) -> list[dict]:
        """Json-serialisable view for the /api/v2/auto_evo/learned_skills endpoint.

        When ``include_disabled`` is True, this also walks the skills
        root and tags any directory whose SKILL.md sets
        ``disabled: true`` / ``enabled: false`` with ``disabled=True``,
        so the UI can render a parked-but-not-deleted entry. Active
        entries get ``disabled=False``.
        """
        active = self.list_skills()
        active_ids = {s.skill_id for s in active}
        # B-158: count older versions on disk that got deduped, so the
        # UI can show "v38 (latest, +5 older versions)".
        older_versions: dict[str, list[dict]] = {}
        for root in self.all_roots:
            if not root.is_dir():
                continue
            try:
                for entry in sorted(root.iterdir()):
                    if not entry.is_dir():
                        continue
                    base, n = _split_versioned(entry.name)
                    if n is None:
                        continue
                    if entry.name in active_ids:
                        continue
                    older_versions.setdefault(base, []).append({
                        "skill_id": entry.name,
                        "version": n,
                        "path": str(entry / "SKILL.md"),
                    })
            except OSError:
                continue
        # Sort each base's older versions descending by N.
        for base in older_versions:
            older_versions[base].sort(key=lambda d: d["version"], reverse=True)

        out: list[dict] = []
        for s in active:
            base, n = _split_versioned(s.skill_id)
            out.append({
                "skill_id": s.skill_id,
                "title": s.title,
                "description": s.description,
                "triggers": s.triggers,
                "source_path": str(s.source_path),
                "mtime": s.mtime,
                "body_preview": s.body[:300],
                "disabled": False,
                # B-158: expose version metadata
                "base_id": base,
                "version": n,
                "older_versions": older_versions.get(base, []) if n is not None else [],
            })
        if not include_disabled:
            return out

        # B-149: walk every scanned root, not just the primary, so
        # disabled SKILL.md from skills.sh / Claude Code paths also
        # show up in the UI as parked entries.
        roots_to_scan = [r for r in self.all_roots if r.is_dir()]
        if not roots_to_scan:
            return out

        # Walk every root once more, surface skills _load_one rejected
        # because of the disabled flag.
        all_entries: list[Path] = []
        for r in roots_to_scan:
            try:
                all_entries.extend(sorted(r.iterdir()))
            except OSError:
                continue
        for entry in all_entries:
            if not entry.is_dir() or entry.name in active_ids:
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")
                stat = skill_md.stat()
            except OSError:
                continue
            fm, body = _parse_frontmatter(text)
            disabled_flag = (
                str(fm.get("disabled", "")).strip().lower() in ("true", "yes", "1", "on")
                or str(fm.get("enabled", "")).strip().lower() in ("false", "no", "0", "off")
            )
            if not disabled_flag:
                continue  # not parked — must be hidden for some other reason
            triggers_raw = fm.get("signals_match") or fm.get("triggers") or []
            triggers = (
                [str(t) for t in triggers_raw if str(t).strip()]
                if isinstance(triggers_raw, list)
                else ([str(triggers_raw)] if str(triggers_raw).strip() else [])
            )
            out.append({
                "skill_id": entry.name,
                "title": str(fm.get("name") or _first_heading(body) or entry.name),
                "description": str(fm.get("description") or _first_paragraph(body)),
                "triggers": triggers,
                "source_path": str(skill_md),
                "mtime": stat.st_mtime,
                "body_preview": body[:300],
                "disabled": True,
            })
        return out


# Module-level singleton — used by the persona writeback helper in
# factory.py to render the section on every system-prompt rebuild.
_default_loader: LearnedSkillsLoader | None = None


def default_learned_skills_loader() -> LearnedSkillsLoader:
    """Return the process-wide LearnedSkillsLoader.

    Wired with workspace + profile-dir providers so SKILL.md template
    tokens (``${XMC_WORKSPACE}`` / ``${XMC_PROFILE_DIR}``) resolve at
    load time. ``inline_shell`` flag pulled from app config —
    defaults False because exec-on-load is risky for auto-generated
    skills. Set ``evolution.auto_evo.inline_shell.enabled=true`` to
    opt in.
    """
    global _default_loader
    if _default_loader is None:
        from xmclaw.daemon.auto_evo_bridge import auto_evo_workspace

        # Best-effort config read — _LAST_APP_STATE may not be set yet
        # when the loader is first instantiated (e.g. tests).
        inline_enabled = False
        try:
            from xmclaw.daemon import app as _app_mod
            state = getattr(_app_mod, "_LAST_APP_STATE", None)
            cfg = getattr(state, "config", None) if state else None
            if isinstance(cfg, dict):
                evo = (cfg.get("evolution") or {}).get("auto_evo") or {}
                shell_cfg = evo.get("inline_shell") or {}
                inline_enabled = bool(shell_cfg.get("enabled", False))
        except Exception:  # noqa: BLE001
            inline_enabled = False

        def _ws_provider():
            try:
                from xmclaw.core.workspace import WorkspaceManager
                ws = WorkspaceManager().get()
                return ws.primary.path if ws.primary is not None else None
            except Exception:  # noqa: BLE001
                return None

        def _profile_provider():
            try:
                from xmclaw.utils.paths import persona_dir
                return persona_dir().parent / "profiles" / "default"
            except Exception:  # noqa: BLE001
                return None

        # B-162: 路径就一个 — ~/.xmclaw/auto_evo/skills/。装在哪、扫
        # 在哪、用在哪全是这一个。不再扫 ~/.agents/ 或 ~/.claude/ 共
        # 享目录 — XMclaw 自治，安装动作必须落进这个私有目录才生效。
        # 已经在 ~/.agents/ 里的旧 skill 通过 import 按钮一次性搬过来。
        # 高级用户仍可通过 config.evolution.skill_paths.extra 加路径。
        from pathlib import Path as _Path
        _extra_roots: list[_Path] = []
        try:
            from xmclaw.daemon import app as _app_mod
            state = getattr(_app_mod, "_LAST_APP_STATE", None)
            cfg = getattr(state, "config", None) if state else None
            if isinstance(cfg, dict):
                evo = (cfg.get("evolution") or {})
                paths_cfg = evo.get("skill_paths") or {}
                raw_extra = paths_cfg.get("extra")
                if isinstance(raw_extra, list):
                    _extra_roots = [_Path(p).expanduser() for p in raw_extra if isinstance(p, str) and p.strip()]
        except Exception:  # noqa: BLE001
            pass

        _default_loader = LearnedSkillsLoader(
            auto_evo_workspace() / "skills",
            inline_shell_enabled=inline_enabled,
            workspace_provider=_ws_provider,
            profile_dir_provider=_profile_provider,
            extra_roots=_extra_roots,
        )
    return _default_loader


def reset_default_learned_skills_loader() -> None:
    """Test hook."""
    global _default_loader
    _default_loader = None
