"""ClaudePluginSkill — bridge Claude Desktop / IDE plugins into XMclaw.

Claude Desktop plugins (``.claude-plugin`` directories with ``plugin.json``)
use a different format from XMclaw's native ``SKILL.md`` / ``skill.py`` skills.
This wrapper parses ``plugin.json`` and exposes the plugin as a callable
Skill so the agent can read its instructions and tool definitions.

Execution model:
  * The plugin's JS/TS code is NOT executed (XMclaw is a Python runtime).
  * The agent receives the plugin's ``instructions`` + ``tools`` list as
    a markdown procedure and can emulate the behaviour using its own tools.
  * If the plugin ships a ``README.md`` or ``SKILL.md`` alongside
    ``plugin.json``, that text is folded into the procedure for extra context.

This is a best-effort compatibility layer — not a full Claude Desktop runtime.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xmclaw.skills.base import Skill, SkillInput, SkillOutput


@dataclass
class ClaudePluginSkill(Skill):
    """Wrap a Claude Desktop ``plugin.json`` as a callable Skill.

    Fields populated from ``plugin.json``:
      * ``id``      → directory name (skill_id)
      * ``name``    → plugin.json ``name`` or directory name
      * ``version`` → plugin.json ``version`` or "1.0.0"
      * ``body``    → assembled markdown procedure
    """

    id: str
    body: str
    version: int = 1
    skill_dir: str = ""

    async def run(self, inp: SkillInput) -> SkillOutput:
        instructions = self.body
        if self.skill_dir:
            instructions = (
                f"> **SKILL_ROOT** = `{self.skill_dir}`\n\n"
                f"_All relative paths resolve against this directory._\n\n"
                f"{self.body}"
            )
        return SkillOutput(
            ok=True,
            result={
                "kind": "claude_plugin_procedure",
                "skill_id": self.id,
                "instructions": instructions,
                "guidance": (
                    f"Skill {self.id!r} is a Claude Desktop plugin bridged "
                    "into XMclaw. The instructions above describe what the "
                    "plugin does and which tools it exposes. Follow the steps "
                    "using your existing tools (bash / file_read / etc). "
                    "Note: the plugin's native JS/TS code does not execute "
                    "in XMclaw's Python runtime."
                ),
            },
            side_effects=[],
        )


def parse_plugin_json(path: Path) -> dict[str, Any]:
    """Parse a Claude Desktop ``plugin.json`` and return a flat dict.

    Raises ``ValueError`` on malformed JSON or missing required fields.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("plugin.json must be a JSON object")
    return data


def build_skill_from_plugin_json(
    skill_dir: Path,
    plugin_json: Path,
) -> tuple[ClaudePluginSkill, str]:
    """Build a :class:`ClaudePluginSkill` from a ``plugin.json`` file.

    Returns ``(skill, error)``.  ``error`` is non-empty when the JSON
    could not be parsed; ``skill`` is None in that case.
    """
    skill_id = skill_dir.name
    try:
        data = parse_plugin_json(plugin_json)
    except Exception as exc:  # noqa: BLE001
        return None, f"plugin.json invalid: {exc}"  # type: ignore[return-value]

    name = data.get("name") or skill_id
    version_str = str(data.get("version", "1.0.0"))
    # Strip semver suffixes so int() works — "1.2.3" becomes 1, which is
    # fine for our coarse-grained Skill.version contract.
    try:
        version = int(version_str.split(".")[0])
    except ValueError:
        version = 1

    description = data.get("description") or ""
    instructions = data.get("instructions") or ""

    # Assemble a markdown procedure from the plugin metadata.
    lines: list[str] = []
    lines.append(f"# {name}")
    if description:
        lines.append("")
        lines.append(description)
    if instructions:
        lines.append("")
        lines.append("## Instructions")
        lines.append(instructions)

    # Fold in tools list if present.
    tools = data.get("tools")
    if isinstance(tools, list) and tools:
        lines.append("")
        lines.append("## Available Tools")
        for t in tools:
            if isinstance(t, dict):
                t_name = t.get("name", "unnamed")
                t_desc = t.get("description", "")
                lines.append(f"- **{t_name}**: {t_desc}")

    # If the plugin ships a README.md or SKILL.md, fold it in for context.
    for extra_name in ("README.md", "SKILL.md", "README"):
        extra_path = skill_dir / extra_name
        if extra_path.is_file():
            try:
                extra_text = extra_path.read_text(encoding="utf-8", errors="replace")
                if extra_text.strip():
                    lines.append("")
                    lines.append(f"## {extra_name}")
                    lines.append(extra_text.strip())
            except Exception:  # noqa: BLE001
                pass
            break  # only append the first one found

    # Add a compatibility disclaimer.
    lines.append("")
    lines.append(
        "> ⚠️ **Compatibility Note**: This skill originates from a "
        "Claude Desktop plugin (``plugin.json``). XMclaw's Python runtime "
        "cannot execute the plugin's native JS/TS code. The agent should "
        "emulate the described behaviour using its own tools."
    )

    body = "\n".join(lines)
    skill = ClaudePluginSkill(
        id=skill_id,
        body=body,
        version=version,
        skill_dir=str(skill_dir.resolve()),
    )
    return skill, ""
