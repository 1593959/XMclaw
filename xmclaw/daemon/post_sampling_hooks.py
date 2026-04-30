"""Post-sampling hook framework (B-112) — free-code parity.

After every successfully completed turn (final assistant response, no
pending tool calls), fire a chain of registered hooks. Each hook gets
the post-turn context (history, llm provider, persona dir) and may run
its own background work asynchronously without blocking the user's
next prompt.

Free-code uses this to:
  * extractMemories  — each turn end, scan transcript for durable
                       facts, append to MEMORY.md
  * SessionMemory    — maintain a per-session running summary file
  * autoDream        — schedule MEMORY.md compaction when thresholds met
  * PromptSuggestion — speculatively pre-warm the next likely prompt

XMclaw already has Auto-Dream as a cron (B-51); this framework makes
the rest pluggable. The first hook landed in B-112 is
``ExtractMemoriesHook`` — same idea as free-code's, gated by
``evolution.memory.extract_memories.enabled`` (default OFF since it's
one extra LLM call per turn).

Cache-sharing optimisation (the main reason free-code uses a "forked
agent" pattern) is left for a follow-up — the LLM provider needs
explicit cache_breakpoint support, which is a separate plumbing job.
The hook can already run today; cache hit-rate is the future
optimisation.
"""
from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass
from typing import Any

from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HookContext:
    """Snapshot of the just-finished turn passed to every hook.

    Hooks must NOT mutate the history list — it's the live one
    AgentLoop uses for the next turn. Read-only by convention.
    """

    session_id: str
    agent_id: str
    user_message: str
    assistant_response: str
    history: list  # type: ignore[type-arg]   # list[Message]
    llm: Any
    persona_dir: Any   # Path | None
    cfg: dict[str, Any]


class PostSamplingHook(abc.ABC):
    """One pluggable post-turn task. Subclasses implement ``run``."""

    #: Stable id for telemetry / disable-by-config. e.g. "extract_memories".
    id: str = ""

    @abc.abstractmethod
    async def run(self, ctx: HookContext) -> None:
        """Do whatever the hook does. Failures must NOT raise — log
        and swallow, so one broken hook doesn't break the chain."""

    def is_enabled(self, ctx: HookContext) -> bool:
        """Default: enabled. Override to gate on config."""
        return True


class HookRegistry:
    """Ordered list of hooks. AgentLoop calls ``dispatch`` after every
    successful turn (final response with no pending tool calls)."""

    def __init__(self) -> None:
        self._hooks: list[PostSamplingHook] = []

    def register(self, hook: PostSamplingHook) -> None:
        self._hooks.append(hook)

    def hooks(self) -> list[PostSamplingHook]:
        return list(self._hooks)

    async def dispatch(self, ctx: HookContext) -> None:
        """Fire every enabled hook concurrently. Errors logged, never
        propagated. Returns when all hooks settle."""
        coros: list[Any] = []
        for h in self._hooks:
            try:
                if not h.is_enabled(ctx):
                    continue
            except Exception:  # noqa: BLE001
                continue
            coros.append(_safe_run(h, ctx))
        if not coros:
            return
        await asyncio.gather(*coros, return_exceptions=True)


async def _safe_run(hook: PostSamplingHook, ctx: HookContext) -> None:
    try:
        await hook.run(ctx)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "post_sampling_hook.run_failed id=%s err=%s",
            getattr(hook, "id", type(hook).__name__), exc,
        )


# ── ExtractMemoriesHook (B-112 reference impl) ────────────────────────


_EXTRACT_PROMPT = (
    "You are reviewing a chat turn that just ended between a user and "
    "the XMclaw agent. Identify any DURABLE facts worth saving to long-"
    "term memory — user preferences ('prefers Python over Go'), "
    "decisions ('we picked sqlite over postgres'), recurring failure "
    "modes ('build always breaks on missing setuptools'), tool-usage "
    "lessons. Skip ephemeral things (status reports, summaries of "
    "what just happened, restated context).\n\n"
    "Output strict JSON: {\"facts\": [\"fact 1\", \"fact 2\"]}. "
    "Empty list if nothing durable was discussed. No prose."
)


class ExtractMemoriesHook(PostSamplingHook):
    """Each turn end, ask the main LLM whether the just-finished
    exchange contained durable facts worth recording. Writes hits to
    MEMORY.md under the ``## Auto-extracted`` section.

    Gated by ``evolution.memory.extract_memories.enabled`` (default
    OFF — adds one extra LLM call per turn). Skips automatically when
    ``persona_dir`` is unset (no place to write).
    """

    id = "extract_memories"

    def is_enabled(self, ctx: HookContext) -> bool:
        if ctx.persona_dir is None:
            return False
        section = (
            ((ctx.cfg.get("evolution") or {}).get("memory") or {})
            .get("extract_memories") or {}
        )
        return bool(section.get("enabled", False))

    async def run(self, ctx: HookContext) -> None:
        import json

        from xmclaw.providers.llm.base import Message

        excerpt = (
            f"User: {ctx.user_message[:1000]}\n\n"
            f"Assistant: {ctx.assistant_response[:1500]}"
        )
        messages = [
            Message(role="system", content=_EXTRACT_PROMPT),
            Message(role="user", content=excerpt),
        ]
        try:
            resp = await ctx.llm.complete(messages, tools=None)
        except Exception:  # noqa: BLE001
            return
        raw = (getattr(resp, "content", None) or "").strip()
        if not raw:
            return
        # Strict / lenient JSON extraction. Same pattern as
        # relevant_picker._parse_pick_response.
        facts: list[str] = []
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and isinstance(obj.get("facts"), list):
                facts = [str(f).strip() for f in obj["facts"] if str(f).strip()]
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not facts:
            return

        # Append to MEMORY.md under ## Auto-extracted section. Reuses
        # the same _append_under_section helper that ``remember`` uses
        # so dedup + char-cap behaviour is shared.
        try:
            from xmclaw.providers.tool.builtin import (
                PERSONA_CHAR_CAPS,
                _append_under_section,
                enforce_char_cap,
            )
            from xmclaw.utils.fs_locks import atomic_write_text, get_lock
        except Exception:  # noqa: BLE001
            return
        from pathlib import Path

        pdir = Path(str(ctx.persona_dir))
        pdir.mkdir(parents=True, exist_ok=True)
        mfile = pdir / "MEMORY.md"
        async with get_lock(mfile):
            try:
                existing = (
                    mfile.read_text(encoding="utf-8") if mfile.is_file() else ""
                )
                new_text = existing
                import time as _t
                date = _t.strftime("%Y-%m-%d")
                for fact in facts[:5]:  # cap per-turn yield
                    bullet = f"- {date}: {fact.replace(chr(10), ' ').strip()}"
                    new_text = _append_under_section(
                        new_text,
                        section_header="## Auto-extracted",
                        bullet=bullet,
                        placeholder_title="MEMORY.md — what I want to remember next time",
                    )
                cap = PERSONA_CHAR_CAPS.get("MEMORY.md")
                if cap is not None and len(new_text) > cap:
                    new_text = enforce_char_cap(new_text, cap)
                if new_text != existing:
                    atomic_write_text(mfile, new_text)
            except OSError:
                return


def build_default_registry() -> HookRegistry:
    """Default hook chain shipped with the daemon."""
    reg = HookRegistry()
    reg.register(ExtractMemoriesHook())
    return reg


__all__ = [
    "HookContext",
    "PostSamplingHook",
    "HookRegistry",
    "ExtractMemoriesHook",
    "build_default_registry",
]
