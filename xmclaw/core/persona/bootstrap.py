"""Bootstrap prefix — first-run interview pattern.

Direct port of QwenPaw ``src/qwenpaw/agents/prompt.py:323-372``
``build_bootstrap_guidance`` + OpenClaw ``system-prompt.ts:206-214``
``[Bootstrap pending]`` prefix. When ``BOOTSTRAP.md`` is present, the
agent's first reply must follow the bootstrap dialogue — interview the
user, write IDENTITY.md / USER.md, then delete BOOTSTRAP.md.
"""
from __future__ import annotations

from pathlib import Path

from xmclaw.core.persona.loader import has_bootstrap_pending


_PREFIX_TEMPLATE = """\
[Bootstrap pending]

There is a BOOTSTRAP.md file in this workspace. That usually means this is
a fresh install and you have not been given a name, vibe, or relationship
to this user. Before answering normally:

1. Read BOOTSTRAP.md.
2. **FIRST check the USER / PROJECT / DECISIONS sections of this prompt
   (sourced from LanceDB facts). If an ``identity``-kind fact already
   says who you are (e.g. "AI 的名字是 X"), USE THAT NAME directly —
   re-asking would be insulting since the user already told you in a
   prior session. Then jump to step 4.**
3. Only if facts AND IDENTITY.md both lack identity info: run the
   interview dialogue described in BOOTSTRAP.md (don't be robotic — chat).
4. Write what you learned into IDENTITY.md (your name / vibe / emoji)
   and USER.md (the user's name, timezone, what they care about).
5. Delete BOOTSTRAP.md when you're done.

Epic #27 G-08 follow-up (2026-05-19): pre-fix this prefix said "Don't
infer a name or persona from prior context; ask the user" — that
overrode existing LanceDB identity facts and forced the agent to
re-ask known names every fresh session. The check at step 2 is the
fix: facts win over the interview.
"""


def bootstrap_prefix(*, profile_dir: Path, workspace_dir: Path | None) -> str:
    """Return the bootstrap-pending prefix, or ``""`` when no BOOTSTRAP.md.

    Always falls back to ``""`` so callers can unconditionally concatenate
    it with the system prompt without a None-guard. Mirrors
    ``buildAgentUserPromptPrefix`` in OpenClaw — but we put it at the head
    of the system prompt (slot 1) rather than at the head of the user
    message, because we want it stable across hops; the bootstrap
    interview is a session-level mode, not a per-turn mode.
    """
    if not has_bootstrap_pending(
        profile_dir=profile_dir, workspace_dir=workspace_dir
    ):
        return ""
    return _PREFIX_TEMPLATE
