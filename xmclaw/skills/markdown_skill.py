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
Auto-promote is gated through evidence-based ``SkillRegistry.promote()``
(anti-req #12) — no path goes from "agent wrote a SKILL.md" to "agent
runs it next turn" without passing through the grader-driven
controller decision.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from xmclaw.skills.base import Skill, SkillInput, SkillOutput

# Strip leading YAML frontmatter (``---\n...\n---\n``) when present.
# Many skills.sh / Claude Code SKILL.md files start with a frontmatter
# block carrying ``description`` / ``name`` / ``signals_match``; the
# agent doesn't need it inline (and it's noisy in tool output).
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)

# Wave-33: structured procedure — JSON block inside markdown body.
# If the body contains a fenced JSON block with a "steps" array,
# the skill returns a machine-readable workflow instead of raw text.
_STRUCTURED_BLOCK_RE = re.compile(
    r"```json\s*(\{.*?\})\s*```", re.DOTALL,
)


def _extract_structured_steps(body: str) -> list[dict[str, Any]] | None:
    """Extract a structured steps array from a markdown body.

    Looks for the first fenced JSON block containing
    ``{"steps": [...]}``.  Each step must have at least ``action``
    (the tool/skill name) and may carry ``args`` and ``note``.

    Returns None when no structured block is found or parsing fails.
    """
    for m in _STRUCTURED_BLOCK_RE.finditer(body or ""):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        steps = data.get("steps")
        if isinstance(steps, list) and steps:
            # Validate minimal step shape.
            valid = []
            for s in steps:
                if isinstance(s, dict) and "action" in s:
                    valid.append(s)
            if valid:
                return valid
    return None


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
    skill_dir: str = ""  # Absolute path to the skill directory for resource resolution

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
        #
        # B-273: scan the SKILL.md body for prompt-injection patterns
        # before handing it back as authoritative instructions. Skills
        # from ~/.agents/skills/ come from the open marketplace
        # (npx skills add); a hostile skill author can stage
        # "ignore all previous instructions and ..." inside the body,
        # and the LLM would obey it as if XMclaw's owner wrote it.
        # The skill_body source has the role-marker patterns
        # suppressed (markdown legitimately contains "Step:" / "Use
        # when:" / etc) but instruction_override / exfiltration are
        # still active.
        body = self.stripped_body
        try:
            from xmclaw.security import (
                PolicyMode,
                SOURCE_SKILL_BODY,
                apply_policy,
            )
            # Fix audit 2026-06-11: use BLOCK for untrusted skills
            # (marketplace installs, user-proposed), DETECT_ONLY for
            # trusted ones. Previously all skills ran DETECT_ONLY,
            # meaning injection findings were never acted upon.
            trust = getattr(self, "_trust_level", None)
            if trust is None:
                try:
                    trust = self.manifest.get("trust_level", "installed")
                except Exception:
                    trust = "installed"
            policy = PolicyMode.BLOCK if trust in ("untrusted",) else PolicyMode.DETECT_ONLY

            decision = apply_policy(
                body,
                policy=policy,
                source=SOURCE_SKILL_BODY,
                extra={"skill_id": self.id, "trust_level": trust},
            )
            if decision.blocked:
                return SkillOutput(
                    ok=False,
                    result=None,
                    error=(
                        f"Skill '{self.name}' body blocked by prompt-injection "
                        f"policy (trust_level={trust}). Review the SKILL.md "
                        f"for instruction-override or exfiltration patterns."
                    ),
                )
            body = decision.content
        except Exception:  # noqa: BLE001 — never break a skill on scan failure
            pass

        # Epic #27 G-09 (2026-06-06): inject SKILL_ROOT path so the LLM
        # can resolve relative references (scripts/, assets/, references/)
        # in the procedure body. Without this, the agent sees
        # ``python3 "$SKILL_ROOT/scripts/scan.py"`` but has no way to
        # know where the skill directory actually lives.
        instructions = body
        if self.skill_dir:
            instructions = (
                f"> **SKILL_ROOT** = `{self.skill_dir}`\n\n"
                f"_All relative paths (scripts/, assets/, references/) "
                f"below resolve against this directory._\n\n"
                f"{body}"
            )

        # Wave-33: structured procedure support.
        steps = _extract_structured_steps(body)
        if steps is not None:
            return SkillOutput(
                ok=True,
                result={
                    "kind": "structured_procedure",
                    "skill_id": self.id,
                    "steps": steps,
                    "instructions": instructions,
                    "guidance": (
                        f"Skill {self.id!r} loaded successfully. "
                        "This skill exposes a structured workflow. "
                        "Use 'skill_compose' or execute the steps "
                        "manually using the listed actions."
                    ),
                },
                side_effects=[],
            )

        return SkillOutput(
            ok=True,
            result={
                "kind": "markdown_procedure",
                "skill_id": self.id,
                "instructions": instructions,
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
