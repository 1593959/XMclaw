"""MarkdownProcedureSkill — Epic #24 Phase 5 (immediate fix).

Phase 1 deleted the multi-path SKILL.md scanner ("path unification")
but only kept the Python-class entrypoint via :class:`UserSkillsLoader`.
Users following the skills.sh convention (``npx skills add`` →
``~/.agents/skills/<name>/SKILL.md``) ended up with files XMclaw
literally couldn't see, even though the agent confidently reported
"already installed" — the exact "split paths" pain point users
flagged.

This wrapper closes the gap: any directory under
``~/.xmclaw/skills_user/<id>/`` that contains a ``SKILL.md`` instead
of a ``skill.py`` becomes a registered skill whose
:meth:`run` returns the procedure body. ``SkillToolProvider`` (already
in production) bridges that into a ``skill_<id>`` tool the LLM picks
like any other.

The wrapper is intentionally tiny: it does NOT try to interpret the
procedure. The agent reads the body as instructions and executes them
using its existing tools (file_read / bash / etc.) on its next turn.
This is the same execution model the deleted xm-auto-evo path used —
just without the multi-root scanning, the ungated "auto-promote"
behaviour, and the silent-fail when files landed in the wrong dir.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from xmclaw.skills.base import Skill, SkillInput, SkillOutput

# Strip leading YAML frontmatter (``---\n...\n---\n``) when present.
# Many skills.sh / Claude Code SKILL.md files start with a frontmatter
# block carrying ``description`` / ``name`` / ``signals_match``; the
# agent doesn't need it inline (and it's noisy in tool output).
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


@dataclass
class MarkdownProcedureSkill(Skill):
    """Wrap a SKILL.md procedure as a callable Skill.

    ``id`` is the directory name (matches Phase 1 ``skill.py`` rule).
    ``version`` is always 1 — a SKILL.md edit is a new content
    snapshot, not a tracked version. If the user wants versioning
    they can promote v2 by writing a Python skill the regular way.
    """

    id: str
    body: str
    version: int = 1

    @property
    def stripped_body(self) -> str:
        return _FRONTMATTER_RE.sub("", self.body, count=1).strip()

    async def run(self, inp: SkillInput) -> SkillOutput:
        # B-176 (real-data finding): the original ``note`` ("This is
        # a procedure — read the body above and execute the steps
        # using your normal tools...") was misread by the LLM as a
        # FAILURE message ("the skill didn't actually do anything")
        # — black-box probe caught the agent saying "skill 还没实际
        # 装进来" right after a successful invocation. Rephrased to
        # frame the response as the skill's INSTRUCTIONS for THIS
        # turn, so the LLM treats the body as authoritative input
        # rather than a meta-hint.
        return SkillOutput(
            ok=True,
            result={
                "kind": "markdown_procedure",
                "skill_id": self.id,
                "instructions": self.stripped_body,
                "guidance": (
                    f"Skill {self.id!r} loaded successfully. The "
                    "'instructions' field above is the authoritative "
                    "playbook for this user request — follow each step "
                    "directly using your other tools (bash / file_read "
                    "/ etc) and produce the final answer when done."
                ),
            },
            side_effects=[],
        )
