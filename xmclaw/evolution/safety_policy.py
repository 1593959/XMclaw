"""Safety policy pre-forge checks.

These functions run BEFORE the forge (and before VFM scoring) so that a
broken concept never produces a shadow file at all — strictly cheaper than
letting the validator catch it after writing to disk. They are pure: no
I/O, no side effects. The engine is responsible for emitting
``EVOLUTION_REJECTED`` when a check fails.

Policy scope:

* **Name collision** — concept ``name`` must not shadow a built-in tool,
  because skill_match / tool_registry dispatch by name and a collision
  silently hijacks a real tool.
* **Shape** — name/trigger must be non-empty and within sensible length
  bounds. The LLM occasionally emits ``""``, whitespace, or 400-char
  prompts as a ``name`` and the downstream filesystem/registry code
  assumes neither.
* **Trigger integrity** — regex triggers must compile; intent triggers
  must carry a non-empty ``intents`` list. A gene that can never match
  anything is a dead row the evolution framework will still count
  against its budget forever.

Return values follow a uniform shape: ``(ok: bool, reason: str | None)``.
Reasons are short slugs (``"name_collision"``, ``"empty_trigger"``, …)
suitable for journal ``reject_reason`` fields and UI badges.
"""
from __future__ import annotations

import re
from typing import Any

# Keep this list in sync with xmclaw/tools/registry.py's _BUILTIN_TOOLS plus
# the lazy-loaded `skill` tool. Missing a name here just weakens the guard —
# it does NOT corrupt anything — so fail-open is acceptable. But every time
# a new built-in is added, it should also be added here.
BUILTIN_TOOL_NAMES = frozenset({
    "agent", "ask_user", "asr", "bash", "browser", "code_exec",
    "computer_use", "file_edit", "file_read", "file_write", "git",
    "github", "glob", "grep", "mcp", "memory_search", "skill",
    "task", "todo", "tts", "vision", "web_fetch", "web_search",
    # Test harness — still a real registered tool in CI.
    "test",
})

# Prefixes reserved for auto-generated artifact IDs. A concept name that
# looks like an ID would confuse UI and audit logs — the forge already
# produces ``skill_<uuid>``/``gene_<uuid>`` internally.
RESERVED_NAME_PREFIXES = ("skill_", "gene_")

_MAX_NAME_LEN = 120
_MIN_NAME_LEN = 1
_MAX_DESCRIPTION_LEN = 2000
_MAX_TRIGGER_LEN = 256


def _check_name(name: Any) -> tuple[bool, str | None]:
    if not isinstance(name, str):
        return False, "name_not_string"
    stripped = name.strip()
    if not stripped:
        return False, "name_empty"
    if len(stripped) < _MIN_NAME_LEN or len(stripped) > _MAX_NAME_LEN:
        return False, "name_length_out_of_bounds"
    # Compare name collisions case-insensitively — the LLM likes to
    # capitalise randomly and the registry dispatch is also lowercase-ish.
    lowered = stripped.lower()
    if lowered in BUILTIN_TOOL_NAMES:
        return False, "name_collision_with_builtin"
    # Only reject if the whole name IS `skill_<something>` etc. Don't
    # reject a name that happens to *contain* those letters.
    for prefix in RESERVED_NAME_PREFIXES:
        if lowered.startswith(prefix):
            return False, "name_uses_reserved_prefix"
    return True, None


def check_skill_concept(concept: dict[str, Any]) -> tuple[bool, str | None]:
    """Run pre-forge safety checks on a skill concept.

    Called from ``EvolutionEngine._generate_skill`` immediately after the
    dedup guard and BEFORE VFM scoring.
    """
    ok, reason = _check_name(concept.get("name"))
    if not ok:
        return ok, reason
    desc = concept.get("description")
    if isinstance(desc, str) and len(desc) > _MAX_DESCRIPTION_LEN:
        return False, "description_too_long"
    return True, None


def check_gene_concept(concept: dict[str, Any]) -> tuple[bool, str | None]:
    """Run pre-forge safety checks on a gene concept.

    Called from ``EvolutionEngine._generate_gene`` immediately after the
    LLM produces ``concept`` and BEFORE VFM scoring.
    """
    ok, reason = _check_name(concept.get("name"))
    if not ok:
        return ok, reason
    desc = concept.get("description")
    if isinstance(desc, str) and len(desc) > _MAX_DESCRIPTION_LEN:
        return False, "description_too_long"

    trigger_type = str(concept.get("trigger_type") or "keyword").lower()
    trigger = concept.get("trigger")
    intents = concept.get("intents")

    # keyword / event / regex all require a non-empty trigger string.
    # Intent triggers are allowed to have an empty trigger AS LONG AS the
    # intents list is non-empty.
    if trigger_type == "intent":
        if not (isinstance(intents, list) and any(
            isinstance(i, str) and i.strip() for i in intents
        )):
            return False, "intent_trigger_requires_intents"
    else:
        if not isinstance(trigger, str) or not trigger.strip():
            return False, "empty_trigger"
        if len(trigger) > _MAX_TRIGGER_LEN:
            return False, "trigger_too_long"

    if trigger_type == "regex":
        assert isinstance(trigger, str)  # narrowed above
        try:
            re.compile(trigger)
        except re.error:
            return False, "invalid_regex_trigger"

    return True, None
