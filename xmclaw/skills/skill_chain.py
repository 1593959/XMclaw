"""Skill chaining — result-driven autonomous skill pipelines.

When skill A completes successfully, its output is scanned for signals
that should trigger skill B. Three chain modes:

  explicit   — skill A's output declares: {"chain_next": "skill_id"}
  pattern    — skill A's output matches a regex → trigger skill B
  always     — skill A always triggers skill B on completion

Chains are registered via manifest under ``chaining.next`` and
``chaining.pattern``. The chain executor runs after every skill
invocation.

Reference: LangChain sequential chains, crewAI task delegation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


@dataclass
class ChainRule:
    source_skill: str
    target_skill: str
    mode: str = "always"  # always / explicit / pattern
    pattern: str = ""  # regex (mode=pattern only)
    max_depth: int = 3  # prevent infinite loops


class SkillChainEngine:
    """Manages skill-to-skill chaining rules."""

    def __init__(self) -> None:
        self._rules: list[ChainRule] = []
        self._active_chain_depth: dict[str, int] = {}

    def register(self, skill_id: str, manifest: dict[str, Any]) -> None:
        chaining = manifest.get("chaining", {})
        if not chaining:
            return
        next_skills = chaining.get("next", [])
        if isinstance(next_skills, str):
            next_skills = [next_skills]

        for target in next_skills:
            if isinstance(target, str):
                rule = ChainRule(
                    source_skill=skill_id,
                    target_skill=target,
                    mode="always",
                )
                self._rules.append(rule)
                _log.info("skill_chain.registered %s → %s (always)", skill_id, target)
            elif isinstance(target, dict):
                rule = ChainRule(
                    source_skill=skill_id,
                    target_skill=target.get("skill", target.get("id", "")),
                    mode=target.get("mode", "always"),
                    pattern=target.get("pattern", ""),
                    max_depth=int(target.get("max_depth", 3)),
                )
                self._rules.append(rule)
                _log.info("skill_chain.registered %s → %s (%s)", skill_id, rule.target_skill, rule.mode)

    def unregister(self, skill_id: str) -> None:
        self._rules = [r for r in self._rules if r.source_skill != skill_id and r.target_skill != skill_id]

    def evaluate(
        self,
        source_skill: str,
        result: Any,
        *,
        current_depth: int = 0,
    ) -> list[str]:
        """Return target skill IDs that should fire after source_skill completes."""
        chain_key = f"{source_skill}:{current_depth}"
        self._active_chain_depth[chain_key] = current_depth
        triggered: list[str] = []

        for rule in self._rules:
            if rule.source_skill != source_skill:
                continue
            if current_depth >= rule.max_depth:
                continue

            if rule.mode == "always":
                triggered.append(rule.target_skill)
            elif rule.mode == "explicit":
                result_str = ""
                if isinstance(result, str):
                    result_str = result
                elif isinstance(result, dict):
                    result_str = result.get("chain_next", "")
                if result_str and result_str.strip():
                    triggered.append(rule.target_skill)
            elif rule.mode == "pattern":
                if rule.pattern:
                    text = ""
                    if isinstance(result, dict):
                        try:
                            text = json.dumps(result)
                        except Exception:
                            text = str(result)
                    elif isinstance(result, str):
                        text = result
                    if re.search(rule.pattern, text):
                        triggered.append(rule.target_skill)

        return triggered
