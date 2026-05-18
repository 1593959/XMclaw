"""HookEngine — composes registered hooks and dispatches lifecycle events.

Wiring story:

  1. ``factory.build_hook_engine_from_config`` reads ``config.hooks``
     and registers HookSpec entries with the engine.
  2. ``agent_loop.py`` / ``hop_loop.py`` / channel adapters / etc.
     call ``await engine.dispatch(HookEvent.X, payload=...)`` at
     the lifecycle moment.
  3. The engine filters hooks by event + matchers, fires every
     matching hook concurrently (via ``asyncio.gather``), collects
     results, and reduces them to a single composite outcome.

Failure isolation: one broken hook raises → swallowed + logged,
other hooks still fire. Mirrors ``post_sampling_hooks._safe_run``.

Matcher language (kept tiny on purpose):

  * keys are payload dict keys (top-level only)
  * values are exact-match strings OR sequences (any-of)
  * missing key in payload → matcher fails
  * empty matcher dict → always matches

Examples:

    matchers: {"tool_name": "bash"}
    matchers: {"tool_name": ["bash", "file_write"]}
    matchers: {"role": "user"}
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import Any

from xmclaw.core.hooks.context import (
    Decision,
    HookContext,
    HookResult,
    merge_decisions,
)
from xmclaw.core.hooks.events import HookEvent
from xmclaw.core.hooks.runners import (
    AgentRunner,
    CommandRunner,
    FunctionRunner,
    HookSpec,
    HttpRunner,
    PromptRunner,
    _BaseRunner,
)
from xmclaw.core.hooks.trust import workspace_trust_level
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


def _matches(spec: HookSpec, payload: dict[str, Any]) -> bool:
    """Apply spec.matchers to payload. See module docstring."""
    if not spec.matchers:
        return True
    for key, want in spec.matchers.items():
        got = payload.get(key)
        if isinstance(want, (list, tuple, set, frozenset)):
            if got not in want:
                return False
        else:
            if got != want:
                return False
    return True


@dataclasses.dataclass
class DispatchOutcome:
    """Combined result of dispatching one event through all matching
    hooks. Aggregates per-hook results into a single decision +
    chain of system messages / output."""

    event: HookEvent
    fired_count: int = 0
    decision: Decision | None = None
    continue_: bool = True
    block_reason: str = ""
    system_messages: list[str] = dataclasses.field(default_factory=list)
    outputs: list[str] = dataclasses.field(default_factory=list)
    updated_input: Any = None  # last non-None overrides
    elapsed_ms: float = 0.0
    per_hook: list[HookResult] = dataclasses.field(default_factory=list)


class HookEngine:
    """Registry + dispatcher.

    Construct once at daemon boot; each subsystem holds the same
    instance via ``app.state.hook_engine``.
    """

    def __init__(
        self,
        *,
        llm_provider: Any | None = None,
        agent_inter: Any | None = None,
        workspace_root: str | None = None,
    ) -> None:
        self._specs: list[HookSpec] = []
        self._workspace_root = workspace_root
        # Per-kind runner instances (LLM-bound ones share the daemon
        # primary).
        self._runners: dict[str, _BaseRunner] = {
            "command": CommandRunner(),
            "function": FunctionRunner(),
            "http": HttpRunner(),
            "prompt": PromptRunner(llm_provider=llm_provider),
            "agent": AgentRunner(agent_inter=agent_inter),
        }

    def register(self, spec: HookSpec) -> None:
        """Add a hook. Idempotent on (id, event) — replacing the same
        id rewrites the spec."""
        self._specs = [
            s for s in self._specs
            if not (s.id == spec.id and s.event == spec.event)
        ]
        self._specs.append(spec)

    def specs(self) -> list[HookSpec]:
        return list(self._specs)

    def clear(self) -> None:
        self._specs.clear()

    async def dispatch(
        self,
        event: HookEvent,
        *,
        session_id: str = "",
        agent_id: str = "main",
        payload: dict[str, Any] | None = None,
        hop: int = -1,
    ) -> DispatchOutcome:
        """Fire every matching hook concurrently and reduce results.

        Hot path on PreLLM / PreToolUse — keep cheap when no hooks
        match (the matching loop is the only cost).
        """
        payload = payload or {}
        matching: list[HookSpec] = [
            s for s in self._specs
            if s.event == event.value and _matches(s, payload)
        ]
        if not matching:
            return DispatchOutcome(event=event)

        ctx = HookContext(
            event=event,
            session_id=session_id,
            agent_id=agent_id,
            payload=payload,
            workspace_root=self._workspace_root,
            workspace_trust=workspace_trust_level(self._workspace_root),
            ts=time.time(),
            hop=hop,
        )
        t0 = time.perf_counter()

        async def _run_one(spec: HookSpec) -> HookResult:
            runner = self._runners.get(spec.runner)
            if runner is None:
                return HookResult(
                    hook_id=spec.id,
                    output=f"[hook {spec.id} unknown runner: {spec.runner}]",
                )
            try:
                return await runner.run(spec, ctx)
            except Exception as exc:  # noqa: BLE001 — isolation
                _log.warning(
                    "hook.runner_raised id=%s runner=%s err=%s",
                    spec.id, spec.runner, exc,
                )
                return HookResult(
                    hook_id=spec.id,
                    output=f"[hook {spec.id} runner crash: {exc}]",
                )

        results = await asyncio.gather(*(_run_one(s) for s in matching))

        outcome = DispatchOutcome(
            event=event,
            fired_count=len(results),
            per_hook=list(results),
            elapsed_ms=(time.perf_counter() - t0) * 1000.0,
        )
        outcome.decision = merge_decisions(results)
        # ``continue_=False`` from any hook stops the lifecycle.
        # Pick the first hook's reason for the operator message.
        for r in results:
            if not r.continue_:
                outcome.continue_ = False
                outcome.block_reason = r.reason or (
                    f"[hook {r.hook_id} requested stop]"
                )
                break
        for r in results:
            if r.system_message:
                outcome.system_messages.append(r.system_message)
            if r.output:
                outcome.outputs.append(r.output)
            if r.updated_input is not None:
                outcome.updated_input = r.updated_input
        return outcome


def build_hook_engine_from_config(
    cfg: dict[str, Any] | None,
    *,
    llm_provider: Any | None = None,
    agent_inter: Any | None = None,
    workspace_root: str | None = None,
) -> HookEngine:
    """Parse ``cfg.hooks`` (a list of dicts) into a populated engine.

    Schema per entry (matches Claude Code shape):

        {
          "id":       "log-prompts",        # required, stable
          "event":    "UserPromptSubmit",   # required, see HookEvent
          "runner":   "command",            # required, one of 5 kinds
          "timeout_s": 5.0,                 # optional, default 5s
          "matchers": {"tool_name": "..."}, # optional
          # runner-specific config:
          "command":  "node ./hook.js",     # for command runner
          "entry":    "myhooks:main",       # for function runner
          "url":      "https://...",        # for http runner
          "prompt":   "Should I block …?",  # for prompt runner
          "agent_id": "research-agent",     # for agent runner
        }

    Empty / missing block → empty engine (no-op for all dispatches).
    """
    engine = HookEngine(
        llm_provider=llm_provider,
        agent_inter=agent_inter,
        workspace_root=workspace_root,
    )
    raw = (cfg or {}).get("hooks")
    if not isinstance(raw, list):
        return engine
    from xmclaw.core.hooks.events import parse_event
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            _log.warning("hook.config_entry_not_dict idx=%d", i)
            continue
        hook_id = str(entry.get("id") or f"hook_{i}")
        event_str = entry.get("event")
        event = parse_event(event_str) if event_str else None
        if event is None:
            _log.warning(
                "hook.unknown_event id=%s event=%r — skipped",
                hook_id, event_str,
            )
            continue
        runner = str(entry.get("runner") or "")
        if runner not in {"command", "function", "http", "prompt", "agent"}:
            _log.warning(
                "hook.unknown_runner id=%s runner=%r — skipped",
                hook_id, runner,
            )
            continue
        # Build runner-config from the entry, minus the well-known
        # top-level fields.
        config_keys = {
            k: v for k, v in entry.items()
            if k not in {"id", "event", "runner", "timeout_s", "matchers"}
        }
        try:
            timeout = float(entry.get("timeout_s") or 5.0)
        except (TypeError, ValueError):
            timeout = 5.0
        matchers = entry.get("matchers") or {}
        if not isinstance(matchers, dict):
            matchers = {}
        spec = HookSpec(
            id=hook_id,
            event=event.value,
            runner=runner,
            timeout_s=timeout,
            matchers=matchers,
            config=config_keys,
        )
        engine.register(spec)
        _log.info(
            "hook.registered id=%s event=%s runner=%s",
            hook_id, event.value, runner,
        )
    return engine


__all__ = [
    "HookEngine",
    "DispatchOutcome",
    "build_hook_engine_from_config",
]
