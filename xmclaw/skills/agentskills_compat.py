"""agentskills.io standard compatibility layer.

Implements the Agent Skills open standard (v1.0.0+) for XMclaw:

1. **Skill Catalog** (Tier 1 progressive disclosure) — generates a
   compact name+description list for the system prompt so the LLM
   knows what skills are available without loading full bodies.

2. **Discovery Index** — emits ``/.well-known/agent-skills/index.json``
   compatible with the agentskills.io discovery spec (RFC 8615
   well-known URI). External tools / agents can scan this file to
   discover XMclaw's installed skills.

3. **Frontmatter validation** — helpers to ensure SKILL.md files
   written by XMclaw (or imported from skills.sh) carry the required
   ``name`` and ``description`` fields.

XMclaw-specific extensions (permissions, trust_level, etc.) are
layered under a ``metadata.xmclaw`` block so agents that don't
understand them can safely ignore them.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from xmclaw.skills.registry import SkillRegistry
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)

# agentskills.io discovery schema version
_DISCOVERY_SCHEMA = "https://schemas.agentskills.io/discovery/0.2.0/schema.json"


class AgentSkillsCatalog:
    """Tier-1 progressive disclosure: a lightweight catalog of skill
    names + descriptions suitable for injection into the system prompt.

    Token budget: ~50-100 tokens per skill. With the default 20-skill
    unified threshold this stays under ~2K tokens.
    """

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def build(self, max_skills: int = 50) -> str:
        """Return a markdown catalog block.

        Format (agentskills.io compliant)::

            ## Available Skills
            - **name** — description
            - **name** — description
            ...
        """
        lines: list[str] = ["## Available Skills"]
        count = 0
        for sid in self._registry.list_skill_ids():
            if count >= max_skills:
                lines.append(f"… and {len(self._registry.list_skill_ids()) - max_skills} more")
                break
            try:
                ref = self._registry.get_ref(sid)
                title = ref.manifest.title or sid
                desc = ref.manifest.description or ""
                if desc:
                    lines.append(f"- **{title}** — {desc}")
                else:
                    lines.append(f"- **{title}**")
                count += 1
            except Exception:  # noqa: BLE001
                continue
        if count == 0:
            return ""
        return "\n".join(lines)


class AgentSkillsIndex:
    """Generate agentskills.io discovery ``index.json``.

    The index lives at ``<data_dir>/.well-known/agent-skills/index.json``
    and follows the v0.2.0 discovery schema.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        data_dir: str | None = None,
        base_url: str = "",
    ) -> None:
        self._registry = registry
        self._data_dir = data_dir or os.path.expanduser("~/.xmclaw")
        self._base_url = base_url.rstrip("/")

    def build(self) -> dict[str, Any]:
        """Return the discovery index dict."""
        skills: list[dict[str, Any]] = []
        for sid in self._registry.list_skill_ids():
            try:
                ref = self._registry.get_ref(sid)
                skill_entry = self._skill_to_entry(sid, ref)
                if skill_entry:
                    skills.append(skill_entry)
            except Exception:  # noqa: BLE001
                continue
        return {
            "$schema": _DISCOVERY_SCHEMA,
            "skills": skills,
        }

    def write(self) -> Path:
        """Persist the index to disk. Returns the written path."""
        idx_dir = Path(self._data_dir) / ".well-known" / "agent-skills"
        idx_dir.mkdir(parents=True, exist_ok=True)
        idx_path = idx_dir / "index.json"
        data = self.build()
        with open(idx_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        _log.info(
            "agentskills.index_written path=%s skills=%d",
            idx_path, len(data.get("skills", [])),
        )
        return idx_path

    def _skill_to_entry(self, sid: str, ref: Any) -> dict[str, Any] | None:
        """Map one SkillRef to a discovery-index skill entry."""
        # Resolve skill source path.
        skill_dir = getattr(ref, "skill_dir", "")
        if not skill_dir:
            return None
        skill_md = Path(skill_dir) / "SKILL.md"
        if not skill_md.exists():
            return None

        # Compute SHA-256 digest of the SKILL.md artifact.
        try:
            h = hashlib.sha256(skill_md.read_bytes()).hexdigest()
        except Exception:  # noqa: BLE001
            return None

        # URL: prefer base_url if configured, else file:// absolute path.
        if self._base_url:
            url = f"{self._base_url}/.well-known/agent-skills/{sid}/SKILL.md"
        else:
            url = skill_md.as_uri()

        return {
            "name": _normalize_skill_name(sid),
            "type": "skill-md",
            "description": (ref.manifest.description or "")[:1024],
            "url": url,
            "digest": f"sha256:{h}",
            # XMclaw extension: layered under metadata so non-XMclaw
            # clients ignore it safely.
            "metadata": {
                "xmclaw": {
                    "version": ref.version,
                    "trust_level": str(
                        getattr(ref.manifest, "trust_level", "")
                    ),
                    "permissions_fs": list(
                        getattr(ref.manifest, "permissions_fs", ()) or ()
                    ),
                    "permissions_net": list(
                        getattr(ref.manifest, "permissions_net", ()) or ()
                    ),
                },
            },
        }


def _normalize_skill_name(name: str) -> str:
    """agentskills.io naming rules:
    - 1-64 chars
    - lowercase alphanumeric + hyphens only
    - no leading/trailing/consecutive hyphens
    """
    # Replace dots (XMclaw namespace separator) with hyphens.
    normalized = name.replace(".", "-").replace("_", "-").lower()
    # Strip any non-alphanumeric/hyphen chars.
    import re
    normalized = re.sub(r"[^a-z0-9-]", "", normalized)
    # Collapse consecutive hyphens.
    normalized = re.sub(r"-+", "-", normalized)
    normalized = normalized.strip("-")
    return normalized[:64] or "unnamed-skill"


def validate_skill_md_frontmatter(body: str) -> tuple[bool, list[str]]:
    """Validate that a SKILL.md carries the required agentskills.io
    frontmatter fields.

    Returns ``(is_valid, list_of_issues)``.
    """
    issues: list[str] = []
    import re as _re
    m = _re.search(r"\A---\n(.*?)\n---", body or "", _re.DOTALL)
    if not m:
        issues.append("missing YAML frontmatter block")
        return False, issues

    front = m.group(1)
    keys: set[str] = set()
    for line in front.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key = line.split(":", 1)[0].strip().lower()
            keys.add(key)

    if "name" not in keys:
        issues.append("missing required field: 'name'")
    if "description" not in keys:
        issues.append("missing required field: 'description'")

    return len(issues) == 0, issues


__all__ = [
    "AgentSkillsCatalog",
    "AgentSkillsIndex",
    "validate_skill_md_frontmatter",
    "_normalize_skill_name",
]
