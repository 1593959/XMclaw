"""Persona / soul system — agent identity assembly.

Direct port of OpenClaw's 7-file workspace pattern + Hermes's prompt-builder
sanitizer + QwenPaw's bootstrap interview pattern. See ``docs/DEV_PLAN.md``
§1.1 for the design rationale.

Public API:

* :func:`build_system_prompt` — top-level: produces the full system prompt
  from a persona profile id and an optional workspace dir.
* :func:`load_persona_files` — read the 7 markdown files in priority order
  for a given profile / workspace overlay, return a list of
  ``(filename, content)`` tuples sorted by ``CONTEXT_FILE_ORDER``.
* :func:`bootstrap_prefix` — return the bootstrap-pending guidance string
  when ``BOOTSTRAP.md`` is present, or ``""``.
* :func:`ensure_default_profile` — write the bundled templates into
  ``~/.xmclaw/persona/profiles/default/`` on first install.

The seven file basenames are (priority order — lower wins, OpenClaw
``system-prompt.ts:44-52``)::

    agents.md (10) → soul.md (20) → identity.md (30) → user.md (40)
        → tools.md (50) → bootstrap.md (60) → memory.md (70)

Resolution layers (project overlay wins for a given basename)::

    1. <workspace>/.xmclaw/persona/<basename>          ← project-level
    2. ~/.xmclaw/persona/profiles/<active>/<basename>  ← user profile
    3. xmclaw/core/persona/templates/<basename>        ← built-in (last resort)
"""
from __future__ import annotations

from xmclaw.core.persona.assembler import build_system_prompt
from xmclaw.core.persona.bootstrap import bootstrap_prefix
from xmclaw.core.persona.loader import (
    CONTEXT_FILE_ORDER,
    PERSONA_BASENAMES,
    PersonaFile,
    ensure_default_profile,
    load_persona_files,
)

__all__ = [
    "CONTEXT_FILE_ORDER",
    "PERSONA_BASENAMES",
    "PersonaFile",
    "build_system_prompt",
    "bootstrap_prefix",
    "ensure_default_profile",
    "load_persona_files",
]
