"""ErrorAwareRetryer — second-chance tool wrapper with LLM-guided fixups.

Background
==========

Kimi K2.6's runtime detects failed tool calls and offers the model an
alternative path (different args, alternative tool, skip). That's a
runtime trick, not a model capability — and it dramatically improves
recovery from typos / stale paths / permission slips in agent traces.

Pre-this: when a tool fails, the error string lands in the next LLM
prompt mixed with all other context. The model often can't tell why
it failed without re-reading 50 turns. Lots of context pollution and
the agent often retries with the same wrong args.

This wrapper sits BETWEEN AgentLoop and the underlying ToolProvider.
On a failed tool call, it:

  1. Reads the failure (error string + args).
  2. Asks the LLM (via a tiny structured prompt) for a fix-up: "try
     these new args" / "use this alternative tool" / "skip — the goal
     can be reached differently".
  3. Executes the fixup once. Bubbles success back to AgentLoop AS
     IF the tool worked on the first try.
  4. If still failing, returns the original error UNCHANGED — the
     AgentLoop sees the same thing it would have without this layer.

The key invariant: **strictly no worse than baseline**. Disabled by
config flag (default ON), one failure per call ceiling, hard timeout
on the fixup LLM round-trip.

How it differs from B-17 transient-retry
========================================

B-17 (already in hop_loop.py) retries identical args on transient
errors (timeout / ECONNRESET). That's pure retry-with-no-thinking.

This module retries with **different** args, picked by the LLM —
covers semantic errors (wrong path, wrong tool name, bad enum value)
which B-17 wouldn't touch.

Both layers compose cleanly: B-17 fires first for transient blips,
then ErrorAwareRetryer fires for semantic ones if B-17 also failed.
"""
from __future__ import annotations

import asyncio
import dataclasses as _dc
import json
import re
import time
from typing import Any

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.utils.log import get_logger

logger = get_logger(__name__)


# Same-as-B17 transient patterns we DON'T fire fixup on — they're
# already handled by hop_loop._invoke_single_tool's retry-once layer.
# Fix-up only kicks in for semantic / argument errors.
_TRANSIENT_PATTERNS = (
    "timeout", "timed out", "connection reset", "connection refused",
    "temporarily unavailable", "ECONNRESET", "ECONNREFUSED",
    "ETIMEDOUT", "EAI_AGAIN", "503 ", "502 ", "504 ", "429 ",
    "remote disconnected", "name or service not known",
)


def _is_transient(err: str) -> bool:
    if not err:
        return False
    low = err.lower()
    return any(p.lower() in low for p in _TRANSIENT_PATTERNS)


_FIXUP_PROMPT = """\
A tool call just failed. Your only job: decide the MINIMAL one-step
fixup the agent should try next.

Tool that failed: ``{tool_name}``
Args were: {tool_args}
Error: {tool_error}

Available tools (name + 1-line description):
{tool_catalog}

Output strict JSON, one of THREE shapes:

  1) Retry the same tool with different args:
     {{"action": "retry", "new_args": {{...}}, "reason": "short why"}}

  2) Switch to a different tool (must exist in the catalog above):
     {{"action": "alternative", "new_tool": "name", "new_args": {{...}}, "reason": "short why"}}

  3) Give up — the agent should handle this differently at a higher
     level (e.g. ask the user, or skip this subgoal):
     {{"action": "skip", "reason": "short why"}}

RULES:
  * MUST be valid JSON, no prose, no markdown fences.
  * ``new_args`` must satisfy the original tool's args schema (or
    the alternative tool's). Inferring requires reading the catalog.
  * Don't loop: if the error suggests the goal is impossible from
    here (e.g. "permission denied" on a path the user owns), prefer
    ``skip`` over retrying the same path with different args.
  * Don't be clever: this is a SINGLE shot. The agent will see the
    final outcome regardless.
"""


class ErrorAwareRetryProvider(ToolProvider):
    """Wraps any ``ToolProvider`` with an LLM-guided one-shot fixup
    layer for non-transient tool failures.

    Constructor params:

    * ``inner`` — the underlying ToolProvider (composite of builtins
      + extras). All calls go through this on success path.
    * ``llm`` — small/fast LLM for the fixup prompt. Same shape
      AgentLoop._llm uses.
    * ``timeout_s`` — wall-clock cap on the fixup LLM call. Default 8s.
    * ``enabled`` — kill-switch from config. Default True.

    Composes cleanly with B-17 transient retry (in hop_loop) — that
    fires for transient errors before we see them, this fires for
    semantic ones after.
    """

    def __init__(
        self,
        inner: ToolProvider,
        *,
        llm: Any | None = None,
        timeout_s: float = 8.0,
        enabled: bool = True,
    ) -> None:
        self._inner = inner
        self._llm = llm
        self._timeout_s = max(2.0, float(timeout_s))
        self._enabled = bool(enabled)
        self._fixups_attempted = 0
        self._fixups_succeeded = 0

    def set_llm(self, llm: Any) -> None:
        """Late-bind the LLM. Used by build_agent_from_config so the
        retry wrapper can be constructed by build_tools_from_config
        (LLM-free) and have the LLM plumbed in afterwards."""
        self._llm = llm

    def list_tools(self) -> list[ToolSpec]:
        # Passthrough — fixup is invocation-time only, never advertised
        # as a separate tool to the LLM.
        return self._inner.list_tools()

    async def invoke(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()
        result = await self._inner.invoke(call)
        if (
            not self._enabled
            or self._llm is None
            or result.ok
            or not result.error
            or _is_transient(result.error)
        ):
            return result

        # Attempt one LLM-guided fixup.
        try:
            fixup = await asyncio.wait_for(
                self._ask_fixup(call, result),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            logger.info(
                "retry_aware.fixup_llm_timeout tool=%s", call.name,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "retry_aware.fixup_llm_failed tool=%s err=%s",
                call.name, exc,
            )
            return result

        if not isinstance(fixup, dict):
            return result
        action = str(fixup.get("action", "")).lower()
        self._fixups_attempted += 1

        if action == "skip":
            logger.info(
                "retry_aware.skip tool=%s reason=%r",
                call.name, fixup.get("reason"),
            )
            return result  # caller sees the original error

        if action == "retry":
            new_args = fixup.get("new_args")
            if not isinstance(new_args, dict):
                return result
            retry_call = _dc.replace(call, args=new_args)
            retry_result = await self._inner.invoke(retry_call)
            if retry_result.ok:
                self._fixups_succeeded += 1
                logger.info(
                    "retry_aware.retry_ok tool=%s reason=%r",
                    call.name, fixup.get("reason"),
                )
                return retry_result
            return result

        if action == "alternative":
            new_tool = str(fixup.get("new_tool", "")).strip()
            new_args = fixup.get("new_args")
            if not new_tool or not isinstance(new_args, dict):
                return result
            # Verify the alternative is in our catalogue — don't let the
            # LLM hallucinate tool names.
            catalog_names = {s.name for s in self._inner.list_tools()}
            if new_tool not in catalog_names:
                logger.info(
                    "retry_aware.alt_unknown tool=%s alt=%r",
                    call.name, new_tool,
                )
                return result
            alt_call = _dc.replace(call, name=new_tool, args=new_args)
            alt_result = await self._inner.invoke(alt_call)
            if alt_result.ok:
                self._fixups_succeeded += 1
                logger.info(
                    "retry_aware.alt_ok orig=%s alt=%s reason=%r",
                    call.name, new_tool, fixup.get("reason"),
                )
                return alt_result
            return result

        # Unknown action shape — bubble original.
        return result

    # ── Internals ──────────────────────────────────────────────────

    async def _ask_fixup(
        self, call: ToolCall, result: ToolResult,
    ) -> dict[str, Any] | None:
        from xmclaw.providers.llm.base import Message
        # Build a compact catalogue — only tool name + first 80 chars
        # of description so the prompt stays small even with 60 tools.
        catalog_lines: list[str] = []
        for s in self._inner.list_tools():
            desc = (s.description or "").replace("\n", " ").strip()
            catalog_lines.append(f"  * {s.name}: {desc[:120]}")
        catalog = "\n".join(catalog_lines[:80])  # absolute cap

        prompt = _FIXUP_PROMPT.format(
            tool_name=call.name,
            tool_args=json.dumps(call.args, ensure_ascii=False)[:500],
            tool_error=(result.error or "")[:500],
            tool_catalog=catalog,
        )
        resp = await self._llm.complete([Message(role="user", content=prompt)])
        raw = (getattr(resp, "content", "") or "").strip()
        return _parse_fixup_json(raw)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _parse_fixup_json(raw: str) -> dict[str, Any] | None:
    """Tolerant 2-tier parser. Returns None on total failure."""
    if not raw:
        return None
    candidates = [raw]
    for m in _FENCE_RE.finditer(raw):
        candidates.append(m.group(1).strip())
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(obj, dict) and "action" in obj:
            return obj
    return None


__all__ = ["ErrorAwareRetryProvider"]
