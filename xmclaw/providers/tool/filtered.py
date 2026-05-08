"""FilteredToolProvider — restrict an inner provider to a subset of tools.

B-332. Closes the cron ``enabled_toolsets`` enforcement gap (audit
finding #7): the field has lived in :class:`~xmclaw.core.scheduler.cron.CronJob`
since the Hermes port, gets persisted to ``~/.xmclaw/cron/jobs.json``,
shown in the UI, but no code path actually filtered tools when a
job fired. A scheduled job with ``enabled_toolsets=["web_fetch"]``
got the agent's full tool stack at runtime — the constraint was a
fiction.

Design:

* Pure wrapper. Inner provider stays untouched; we only filter its
  output.
* :meth:`list_tools` returns a subset whose ``name`` is in
  ``allowed_names``. Empty allowlist (``set()``) means "block
  everything"; use ``allowed_names=None`` (or just don't wrap) for
  "no restriction".
* :meth:`invoke` rejects calls to disallowed tools with a structured
  ``ToolResult(ok=False, error=...)`` so the agent loop sees a real
  refusal it can act on (apologise to the user / pick a different
  tool) rather than a silent no-op.

Used by:

* AgentLoop's ``run_turn(..., tools_allowlist=...)`` kwarg —
  per-call wrapping, no shared mutable state. Concurrent turns with
  different filters don't collide.
* Cron runner — passes ``tools_allowlist=set(job.enabled_toolsets)``
  so a ``"every 1h"`` job constrained to ``web_fetch`` actually
  can't reach for ``bash`` mid-fire.
"""
from __future__ import annotations

from collections.abc import Iterable

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider


class FilteredToolProvider(ToolProvider):
    """Wrap ``inner`` to expose only tools whose name is in
    ``allowed_names``. ``invoke`` of a disallowed name returns a
    failure result with a clear error message.

    Parameters
    ----------
    inner : ToolProvider
        The provider whose tools we're filtering. Its results pass
        through unchanged when allowed.
    allowed_names : Iterable[str]
        Tool names the wrapper exposes. Empty iterable → no tool
        passes (every invoke is refused). Non-string entries are
        coerced via ``str()``; duplicates are deduplicated.
    """

    def __init__(
        self, inner: ToolProvider, allowed_names: Iterable[str],
    ) -> None:
        self._inner = inner
        self._allowed: frozenset[str] = frozenset(str(n) for n in allowed_names)

    @property
    def allowed_names(self) -> frozenset[str]:
        """Snapshot of the allow-list. Useful for tests and for the
        agent loop's "what's available this turn" log line."""
        return self._allowed

    def list_tools(self) -> list[ToolSpec]:
        return [spec for spec in self._inner.list_tools() if spec.name in self._allowed]

    async def invoke(self, call: ToolCall) -> ToolResult:
        if call.name not in self._allowed:
            allowed_summary = (
                ", ".join(sorted(self._allowed))
                if self._allowed else "(no tools allowed)"
            )
            return ToolResult(
                call_id=call.id,
                ok=False,
                content=None,
                error=(
                    f"tool {call.name!r} not in current allowlist; "
                    f"allowed: {allowed_summary}"
                ),
            )
        return await self._inner.invoke(call)
