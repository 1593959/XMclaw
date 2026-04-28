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

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


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


def _load_one(skill_dir: Path) -> LearnedSkill | None:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        stat = skill_md.stat()
    except OSError:
        return None
    fm, body = _parse_frontmatter(text)
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
    no-op rebuild is free."""

    def __init__(self, skills_root: Path) -> None:
        self._root = skills_root
        self._cache_key: tuple | None = None
        self._cache_block: str = ""
        self._cache_skills: list[LearnedSkill] = []

    @property
    def skills_root(self) -> Path:
        return self._root

    def _scan(self) -> list[LearnedSkill]:
        if not self._root.is_dir():
            return []
        skills: list[LearnedSkill] = []
        try:
            entries = sorted(self._root.iterdir())
        except OSError:
            return []
        for entry in entries:
            if not entry.is_dir():
                continue
            sk = _load_one(entry)
            if sk is not None:
                skills.append(sk)
        return skills

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
            "当用户的请求 **匹配下面的 trigger** 时，"
            "**优先按对应 SKILL 的步骤执行** — "
            "用你已有的工具（`file_read` / `bash` / `web_search` 等）"
            "去走流程。这是你已经学会的事情，"
            "比从零思考更可靠。"
        )
        lines.append("")
        for sk in skills[:max_skills]:
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
            # First ~600 chars of the body — enough for the agent to
            # see the procedure without bloating the system prompt.
            excerpt = sk.body.strip()
            if excerpt:
                # Strip top-level # heading since we already render
                # it above as ###.
                excerpt_lines = []
                skipped_first_h1 = False
                for ln in excerpt.splitlines():
                    if not skipped_first_h1 and ln.lstrip().startswith("# "):
                        skipped_first_h1 = True
                        continue
                    excerpt_lines.append(ln)
                excerpt = "\n".join(excerpt_lines).strip()[:600]
                if excerpt:
                    lines.append(excerpt)
                    lines.append("")

        block = "\n".join(lines).rstrip()
        self._cache_key = fp
        self._cache_block = block
        self._cache_skills = skills
        return block

    def list_for_api(self) -> list[dict]:
        """Json-serialisable view for the /api/v2/auto_evo/learned_skills endpoint."""
        return [
            {
                "skill_id": s.skill_id,
                "title": s.title,
                "description": s.description,
                "triggers": s.triggers,
                "source_path": str(s.source_path),
                "mtime": s.mtime,
                "body_preview": s.body[:300],
            }
            for s in self.list_skills()
        ]


# Module-level singleton — used by the persona writeback helper in
# factory.py to render the section on every system-prompt rebuild.
_default_loader: LearnedSkillsLoader | None = None


def default_learned_skills_loader() -> LearnedSkillsLoader:
    global _default_loader
    if _default_loader is None:
        from xmclaw.daemon.auto_evo_bridge import auto_evo_workspace
        _default_loader = LearnedSkillsLoader(
            auto_evo_workspace() / "skills",
        )
    return _default_loader


def reset_default_learned_skills_loader() -> None:
    """Test hook."""
    global _default_loader
    _default_loader = None
