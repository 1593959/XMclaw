"""SkillToolProvider — expose registered Skills as first-class tools.

After Epic #24 Phase 1 ripped out the xm-auto-evo SKILL.md prompt
injection path, **this is the only way a skill becomes callable by the
agent**. ``SkillRegistry`` HEAD entries (each one having passed
evidence-gated promote — anti-req #12) get bridged to tools the LLM
picks like any other (``bash`` / ``file_read`` / etc.). Direct text
injection of skill bodies into the system prompt is no longer a path.

This module makes any Skill registered at HEAD show up as a tool named
``skill_<skill_id>``, with a permissive object schema so the LLM can
decide what to pass. Tool invocation routes back to
``registry.get(skill_id).run(SkillInput(args=...))`` — same code path
as direct programmatic use.

Tool naming
-----------

Anthropic / OpenAI tool names must match ``[a-zA-Z0-9_-]{1,64}``. Skill
ids commonly contain ``.`` (``demo.read_and_summarize``) which would be
rejected by the wire schema. We map ``.`` → ``__`` (double underscore
as namespace separator) and prefix with ``skill_`` so collisions with
built-in tools are impossible to accidentally introduce.

  ``demo.read_and_summarize`` → tool name ``skill_demo__read_and_summarize``

The reverse map is built once at construction, so ``invoke`` is O(1).

Layering note
-------------

Lives in ``xmclaw/skills/`` rather than ``xmclaw/providers/tool/`` so
the providers layer doesn't grow a new dependency on the skills
package (see ``xmclaw/providers/tool/AGENTS.md`` §2). Conforms to the
``ToolProvider`` shape structurally — no inheritance — and the daemon
composes it via :class:`CompositeToolProvider` at wiring time.
"""
from __future__ import annotations

import re
import time
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.skills.base import SkillInput
from xmclaw.skills.registry import SkillRegistry, UnknownSkillError


_VALID_NAME = re.compile(r"[^a-zA-Z0-9_-]")


def _to_tool_name(skill_id: str) -> str:
    """Convert a skill_id to a wire-safe tool name.

    Replaces ``.`` with ``__`` to preserve namespace boundaries (the
    LLM sees ``demo.foo`` and ``demo.bar`` as ``skill_demo__foo`` and
    ``skill_demo__bar``, still visibly the same family). Other invalid
    chars get squashed to ``_``.
    """
    safe = skill_id.replace(".", "__")
    safe = _VALID_NAME.sub("_", safe)
    return f"skill_{safe}"[:64]


class SkillToolProvider:
    """Bridges :class:`SkillRegistry` HEAD into a :class:`ToolProvider`.

    The list is dynamically rebuilt on every ``list_tools()`` call, so
    a promote/rollback that moves HEAD is reflected on the next turn
    without restarting the agent. ``invoke`` looks up the current HEAD
    too — never a stale snapshot, never a phantom tool the registry no
    longer has.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        description_prefix: str = "Skill: ",
    ) -> None:
        self._registry = registry
        self._description_prefix = description_prefix

    def _spec_for(self, skill_id: str) -> ToolSpec | None:
        try:
            ref = self._registry.ref(skill_id)
        except UnknownSkillError:
            return None
        manifest = ref.manifest
        # Manifest doesn't currently carry a description or input schema.
        # Use a permissive object schema so the LLM can pass whatever
        # makes sense; future manifest fields can tighten this without
        # touching the bridge.
        description = (
            f"{self._description_prefix}{skill_id} v{ref.version} "
            f"(created_by={manifest.created_by})"
        )
        if manifest.evidence:
            description += f" — evidence: {'; '.join(manifest.evidence)}"
        return ToolSpec(
            name=_to_tool_name(skill_id),
            description=description,
            parameters_schema={
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Arguments forwarded to Skill.run(SkillInput(args=...)). "
                    "Pass whatever fields the skill's run() expects."
                ),
            },
        )

    def list_tools(self) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        for skill_id in self._registry.list_skill_ids():
            spec = self._spec_for(skill_id)
            if spec is not None:
                out.append(spec)
        return out

    async def invoke(self, call: ToolCall) -> ToolResult:
        """Resolve ``call.name`` back to a skill_id and run it.

        Errors surface as ``ToolResult(ok=False, error=...)`` rather
        than raising — the agent loop expects tool failures to come
        through this channel so it can reason about them, not crash
        the turn.
        """
        t0 = time.perf_counter()
        skill_id = self._tool_name_to_skill_id(call.name)
        if skill_id is None:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"unknown skill tool: {call.name!r}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        try:
            skill = self._registry.get(skill_id)
        except UnknownSkillError as exc:
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"skill {skill_id!r} not at HEAD: {exc}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        try:
            out = await skill.run(SkillInput(args=dict(call.args or {})))
        except Exception as exc:  # noqa: BLE001 — surface to the loop
            return ToolResult(
                call_id=call.id, ok=False, content=None,
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        return ToolResult(
            call_id=call.id,
            ok=bool(out.ok),
            content=out.result,
            error=None if out.ok else _coerce_error(out.result),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            side_effects=tuple(out.side_effects or ()),
        )

    def _tool_name_to_skill_id(self, tool_name: str) -> str | None:
        """Walk the current HEAD list looking for the skill_id whose
        encoded name matches. Done by lookup rather than reverse-decoding
        because the encoding is lossy (multiple skill_ids could in
        principle collide on the safe-char form, and we'd rather match
        an actual registry entry than guess)."""
        for sid in self._registry.list_skill_ids():
            if _to_tool_name(sid) == tool_name:
                return sid
        return None


def _coerce_error(result: Any) -> str | None:
    """Pull a human-readable error string out of a SkillOutput.result
    when the skill returned ok=False. Skills typically put ``error`` in
    the dict; fall back to repr otherwise so the agent gets SOMETHING
    to read instead of an empty string."""
    if isinstance(result, dict):
        for key in ("error", "message", "reason"):
            v = result.get(key)
            if isinstance(v, str) and v:
                return v
    return repr(result) if result is not None else "skill returned ok=False"
