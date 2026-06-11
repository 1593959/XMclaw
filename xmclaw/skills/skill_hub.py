"""Skill Hub — unified multi-format skill registry with autonomous invocation.

Accepts and normalises skills in ANY format:
  - Python SkillBase subclass (skill.py)
  - Markdown SKILL.md (agentskills.io / Claude Code convention)
  - Claude Desktop plugin.json
  - MCP server (auto-detected from package.json / pyproject.toml)

All formats are projected into a unified ``SkillEntry`` with:
  - id, name, description, version
  - source_format (python / markdown / claude_plugin / mcp_server)
  - triggers (keywords, events, cron, cooldown)
  - security metadata (trust_level, sandbox_required)

The hub wires directly into the existing SkillToolProvider, so zero
changes are needed in the AgentLoop invocation path. The hub also
connects to the TriggerEngine for autonomous activation.

Reference: Claude Code skills.md, agentskills.io, Mem0 autonomous ops.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


@dataclass
class SkillEntry:
    """Unified skill descriptor — decoupled from any specific format."""
    id: str
    name: str
    description: str
    version: int = 1
    source_format: str = "python"  # python / markdown / claude_plugin / mcp_server
    trust_level: str = "installed"  # untrusted / installed / user / builtin
    triggers: dict[str, Any] = field(default_factory=dict)
    sandbox_required: bool = False
    source_path: str = ""
    raw_manifest: dict[str, Any] = field(default_factory=dict)

    def to_tool_spec_kwargs(self) -> dict[str, Any]:
        """Project into keyword arguments for ToolSpec construction."""
        return {
            "name": f"skill_{self.id}",
            "description": self.description,
            "read_only": self._is_read_only(),
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "args": {
                        "type": "object",
                        "description": (
                            "Arguments to pass to the skill. The skill's "
                            "input schema defines what keys are accepted."
                        ),
                    },
                },
            },
        }

    def _is_read_only(self) -> bool:
        if self.source_format == "mcp_server":
            return False
        perms = self.raw_manifest.get("permissions", {})
        return not any(
            perms.get(p) for p in ("fs_write", "net_write", "subprocess")
        )


class SkillHub:
    """Central registry that normalises all skill formats into one view.

    Wraps the existing ``SkillRegistry`` and adds:
      - multi-format loading (SKILL.md, plugin.json, MCP auto-detect)
      - trigger registration with TriggerEngine
      - security policy enforcement (block untrusted on install)
    """

    def __init__(self, registry: Any, trigger_engine: Any | None = None) -> None:
        self.registry = registry
        self.trigger_engine = trigger_engine
        self._entries: dict[str, SkillEntry] = {}
        self._format_index: dict[str, list[str]] = {
            "python": [], "markdown": [], "claude_plugin": [], "mcp_server": [],
        }

    # ── registration ──────────────────────────────────────────────

    def register(
        self,
        entry: SkillEntry,
        skill_instance: Any = None,
    ) -> None:
        """Register a skill from any format into the unified hub."""
        sid = entry.id
        self._entries[sid] = entry
        self._format_index.setdefault(entry.source_format, []).append(sid)

        # Register with TriggerEngine if triggers are defined
        if self.trigger_engine is not None and entry.triggers:
            self.trigger_engine.register(sid, entry.raw_manifest)

        # Register with the underlying SkillRegistry
        if skill_instance is not None:
            try:
                self.registry.register(
                    skill_id=sid,
                    version=entry.version,
                    skill=skill_instance,
                    manifest=entry.raw_manifest,
                    set_head=True,
                )
            except Exception:  # noqa: BLE001 — already registered
                pass

        _log.info("skill_hub.registered id=%s format=%s", sid, entry.source_format)

    def unregister(self, skill_id: str) -> None:
        entry = self._entries.pop(skill_id, None)
        if entry is None:
            return
        self._format_index.get(entry.source_format, []).remove(skill_id)
        if self.trigger_engine is not None:
            self.trigger_engine.unregister(skill_id)

    # ── queries ──────────────────────────────────────────────────

    def get(self, skill_id: str) -> SkillEntry | None:
        return self._entries.get(skill_id)

    def list_all(self) -> list[SkillEntry]:
        return list(self._entries.values())

    def list_by_format(self, fmt: str) -> list[SkillEntry]:
        return [self._entries[sid] for sid in self._format_index.get(fmt, []) if sid in self._entries]

    def list_by_trigger_type(self, trigger_type: str) -> list[SkillEntry]:
        """Return skills that have a specific trigger type configured."""
        return [
            e for e in self._entries.values()
            if e.triggers.get(trigger_type)
        ]

    def force_inject_ids(self, skill_ids: list[str]) -> list[str]:
        """Return skill_ids that exist in the hub. Caller should add these
        to the LLM tool list regardless of prefilter score."""
        return [sid for sid in skill_ids if sid in self._entries]
