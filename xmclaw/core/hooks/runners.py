"""Hook runners — 5 kinds, each fires the user's logic differently.

Borrowed verbatim from the Claude Code taxonomy:

  * ``command`` — shell command; gets the context as JSON on stdin,
    returns a HookResult parsed from JSON on stdout. Most flexible;
    requires workspace trust.
  * ``function`` — Python entry point (``module.function``); gets
    HookContext directly. Best for performance-sensitive logic
    (no subprocess overhead). Requires workspace trust (same RCE
    surface as ``command``).
  * ``http`` — POST the JSON-serialised context to a URL, parse the
    JSON response as HookResult. Doesn't need workspace trust
    (executes off-host). Useful for "external service approves this".
  * ``prompt`` — ask the daemon's LLM a one-shot question with the
    context substituted in; parse the LLM's reply as a decision.
    The slowest + most expensive runner.
  * ``agent`` — fire-and-forget a sub-agent that runs in parallel
    (uses ``submit_to_agent`` plumbing). Never blocks the lifecycle —
    its result is informational only.

All runners are async + must respect a per-hook ``timeout_s``.
Timeouts produce a non-blocking HookResult (no decision, but logged)
so a stuck command can't freeze the daemon.
"""
from __future__ import annotations

import abc
import asyncio
import dataclasses
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

from xmclaw.core.hooks.context import HookContext, HookResult
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


# ── Base ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HookSpec:
    """One configured hook. Parsed from config.hooks[i]."""

    id: str           # stable identifier (for logs + telemetry)
    event: str        # HookEvent.value
    runner: str       # "command" | "function" | "http" | "prompt" | "agent"
    timeout_s: float = 5.0
    # Optional matchers — only fire when payload matches.
    # E.g. {"tool_name": "bash"} on PreToolUse only fires for bash.
    matchers: dict[str, Any] = dataclasses.field(default_factory=dict)
    # Runner-specific config (command string, module:function, URL, …)
    config: dict[str, Any] = dataclasses.field(default_factory=dict)


class _BaseRunner(abc.ABC):
    """Each runner subclass knows how to execute one HookSpec.kind."""

    @abc.abstractmethod
    async def run(
        self, spec: HookSpec, ctx: HookContext,
    ) -> HookResult: ...

    @staticmethod
    def _pack_context_json(ctx: HookContext) -> str:
        """Serialise the context to the canonical JSON form callers
        receive on stdin / HTTP body."""
        return json.dumps({
            "event": ctx.event.value,
            "session_id": ctx.session_id,
            "agent_id": ctx.agent_id,
            "payload": ctx.payload,
            "workspace_root": ctx.workspace_root,
            "workspace_trust": ctx.workspace_trust,
            "ts": ctx.ts,
            "hop": ctx.hop,
        }, ensure_ascii=False)

    @staticmethod
    def _parse_result_json(
        raw: str, hook_id: str,
    ) -> HookResult:
        """Parse a JSON HookResult from runner stdout / HTTP body.

        Accepts the Claude Code shape (``continue``, ``decision``,
        ``systemMessage``, ``updatedInput``, ``output``, ``reason``)
        with camelCase OR snake_case keys. Missing keys → defaults.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Non-JSON output → treat as plain text output, no
            # decision. Useful for ``echo "blah"`` style debug hooks.
            return HookResult(output=raw.strip(), hook_id=hook_id)
        if not isinstance(data, dict):
            return HookResult(output=str(data), hook_id=hook_id)

        def _pick(*keys: str, default: Any = None) -> Any:
            for k in keys:
                if k in data:
                    return data[k]
            return default

        decision = _pick("decision")
        if decision not in (None, "allow", "deny", "ask"):
            decision = None
        return HookResult(
            continue_=bool(_pick("continue", "continue_", default=True)),
            decision=decision,
            system_message=str(_pick(
                "systemMessage", "system_message", default="",
            ) or ""),
            updated_input=_pick("updatedInput", "updated_input"),
            output=str(_pick("output", default="") or ""),
            reason=str(_pick("reason", default="") or ""),
            hook_id=hook_id,
        )


# ── command runner ────────────────────────────────────────────────


class CommandRunner(_BaseRunner):
    """Shell command. ``spec.config["command"]`` is the cmd line.

    Context is piped to the process's stdin as JSON. Stdout is parsed
    as the HookResult JSON. Non-zero exit code is treated as
    ``continue=False, reason=stderr``.

    Requires workspace_trust == "trusted" — refuses to run otherwise.
    """

    async def run(
        self, spec: HookSpec, ctx: HookContext,
    ) -> HookResult:
        if ctx.workspace_trust != "trusted":
            return HookResult(
                continue_=True,  # don't block on trust-deny — just skip
                output=f"[hook {spec.id} skipped: workspace not trusted]",
                hook_id=spec.id,
            )
        cmd = spec.config.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} misconfigured: no 'command']",
            )
        ctx_json = self._pack_context_json(ctx)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=ctx.workspace_root,
                env={**os.environ, "XMCLAW_HOOK_EVENT": ctx.event.value},
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(ctx_json.encode("utf-8")),
                    timeout=spec.timeout_s,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return HookResult(
                    hook_id=spec.id,
                    output=f"[hook {spec.id} timed out > {spec.timeout_s}s]",
                )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "hook.command_failed id=%s err=%s", spec.id, exc,
            )
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} failed: {exc}]",
            )
        if proc.returncode and proc.returncode != 0:
            return HookResult(
                continue_=False,
                reason=(stderr.decode("utf-8", errors="replace")[:200]
                        or f"exit code {proc.returncode}"),
                hook_id=spec.id,
            )
        return self._parse_result_json(
            stdout.decode("utf-8", errors="replace"), spec.id,
        )


# ── function runner ───────────────────────────────────────────────


class FunctionRunner(_BaseRunner):
    """Python callable. ``spec.config["entry"]`` is ``"module:function"``.

    The callable receives a ``HookContext`` and returns a
    ``HookResult`` (or dict, parsed via _parse_result_json). Async
    callables are awaited; sync ones are scheduled in the default
    executor so a slow function can't block the event loop.

    Requires workspace trust — calling arbitrary user Python is the
    same RCE surface as shell.
    """

    async def run(
        self, spec: HookSpec, ctx: HookContext,
    ) -> HookResult:
        if ctx.workspace_trust != "trusted":
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} skipped: workspace not trusted]",
            )
        entry = spec.config.get("entry") or spec.config.get("function")
        if not isinstance(entry, str) or ":" not in entry:
            return HookResult(
                hook_id=spec.id,
                output=(
                    f"[hook {spec.id} misconfigured: "
                    "'entry' must be 'module:function']"
                ),
            )
        mod_name, fn_name = entry.split(":", 1)
        try:
            import importlib
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name)
        except Exception as exc:  # noqa: BLE001
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} import failed: {exc}]",
            )

        async def _invoke() -> Any:
            if asyncio.iscoroutinefunction(fn):
                return await fn(ctx)
            return await asyncio.to_thread(fn, ctx)

        try:
            result = await asyncio.wait_for(_invoke(), timeout=spec.timeout_s)
        except asyncio.TimeoutError:
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} timed out > {spec.timeout_s}s]",
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "hook.function_failed id=%s err=%s", spec.id, exc,
            )
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} raised: {type(exc).__name__}: {exc}]",
            )
        if isinstance(result, HookResult):
            return dataclasses.replace(result, hook_id=spec.id)
        if isinstance(result, dict):
            return self._parse_result_json(
                json.dumps(result), spec.id,
            )
        return HookResult(
            hook_id=spec.id, output=str(result) if result else "",
        )


# ── http runner ───────────────────────────────────────────────────


class HttpRunner(_BaseRunner):
    """POST the context to a URL, parse JSON response as HookResult.

    Doesn't require workspace trust — the URL is fixed in config so
    the operator already vetted it.
    """

    async def run(
        self, spec: HookSpec, ctx: HookContext,
    ) -> HookResult:
        url = spec.config.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} misconfigured: bad 'url']",
            )
        try:
            import httpx
        except ImportError:
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} skipped: httpx not installed]",
            )
        headers = spec.config.get("headers") or {}
        if not isinstance(headers, dict):
            headers = {}
        ctx_json = self._pack_context_json(ctx)
        try:
            async with httpx.AsyncClient(timeout=spec.timeout_s) as c:
                r = await c.post(url, content=ctx_json, headers={
                    "content-type": "application/json",
                    **{str(k): str(v) for k, v in headers.items()},
                })
            r.raise_for_status()
            return self._parse_result_json(r.text, spec.id)
        except Exception as exc:  # noqa: BLE001
            _log.warning("hook.http_failed id=%s err=%s", spec.id, exc)
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} http failed: {exc}]",
            )


# ── prompt runner (LLM-driven) ────────────────────────────────────


class PromptRunner(_BaseRunner):
    """Ask the daemon's LLM a question and parse the answer as JSON.

    ``spec.config["prompt"]`` is a Python format string; the context
    fields are substituted as ``{event}``, ``{session_id}``,
    ``{payload}`` (JSON), ``{workspace_root}``, ``{hop}``. The
    daemon's primary LLM is used (via the injected provider).

    Slow + expensive. Don't enable on hot-path events (PreLLM /
    PreToolUse) without a tight ``matchers`` constraint.
    """

    def __init__(self, llm_provider: Any | None = None) -> None:
        self._llm = llm_provider

    async def run(
        self, spec: HookSpec, ctx: HookContext,
    ) -> HookResult:
        if self._llm is None:
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} skipped: no LLM wired]",
            )
        template = spec.config.get("prompt")
        if not isinstance(template, str):
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} misconfigured: no 'prompt']",
            )
        text = template.format(
            event=ctx.event.value,
            session_id=ctx.session_id,
            payload=json.dumps(ctx.payload, ensure_ascii=False),
            workspace_root=ctx.workspace_root or "",
            hop=ctx.hop,
        )
        try:
            from xmclaw.core.ir import Message
            msgs = [Message(role="user", content=text)]
            resp = await asyncio.wait_for(
                self._llm.complete(msgs),
                timeout=spec.timeout_s,
            )
            content = getattr(resp, "content", None) or str(resp)
        except asyncio.TimeoutError:
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} llm timeout > {spec.timeout_s}s]",
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("hook.prompt_failed id=%s err=%s", spec.id, exc)
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} llm failed: {exc}]",
            )
        return self._parse_result_json(content, spec.id)


# ── agent runner (fire-and-forget sub-agent) ──────────────────────


class AgentRunner(_BaseRunner):
    """Dispatch a fire-and-forget sub-agent turn.

    Never blocks the lifecycle — returns immediately with a
    ``task_id`` reference. The sub-agent's reply lands in
    ``agent_tasks`` (via the existing ``submit_to_agent`` machinery)
    so the operator can read it asynchronously.

    Won't vote on permission decisions (decision stays None) — by
    construction, since we don't await the agent's reply.
    """

    def __init__(self, agent_inter: Any | None = None) -> None:
        self._agent_inter = agent_inter

    async def run(
        self, spec: HookSpec, ctx: HookContext,
    ) -> HookResult:
        if self._agent_inter is None:
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} skipped: agent_inter not wired]",
            )
        target_agent = spec.config.get("agent_id", "main")
        prompt_template = spec.config.get("prompt", "{payload}")
        if not isinstance(prompt_template, str):
            prompt_template = "{payload}"
        content = prompt_template.format(
            event=ctx.event.value,
            session_id=ctx.session_id,
            payload=json.dumps(ctx.payload, ensure_ascii=False),
            workspace_root=ctx.workspace_root or "",
        )
        try:
            # Use the AgentInterTools internal API to submit without
            # going through the public tool dispatch.
            submit = getattr(self._agent_inter, "submit_background", None)
            if not callable(submit):
                return HookResult(
                    hook_id=spec.id,
                    output=(
                        f"[hook {spec.id} skipped: agent_inter has no "
                        "submit_background]"
                    ),
                )
            task_id = await submit(agent_id=target_agent, content=content)
        except Exception as exc:  # noqa: BLE001
            return HookResult(
                hook_id=spec.id,
                output=f"[hook {spec.id} dispatch failed: {exc}]",
            )
        return HookResult(
            hook_id=spec.id,
            output=f"[hook {spec.id} → agent={target_agent} task_id={task_id}]",
        )


__all__ = [
    "HookSpec",
    "CommandRunner",
    "FunctionRunner",
    "HttpRunner",
    "PromptRunner",
    "AgentRunner",
]
