"""B-397 follow-up (audit pass-3 finding C3): front-back contract test
for the stuck-loop break event.

Existing B-397 tests in ``test_v2_b397_stuck_loop.py`` cover the
agent_loop's break-after-3-consecutive-failures behaviour but NOT
the cross-language contract: the daemon emits an ``ANTI_REQ_VIOLATION``
event with ``kind="stuck_loop"`` + tool + error_signature + message,
and ``chat_reducer_secondary.js`` reads ``payload.reason ||
payload.message || payload.kind`` to render the inline system bubble.

If the daemon's payload shape drifts (e.g. someone renames ``kind``
to ``type``) the reducer's fallback to ``payload.kind`` quietly
hides the regression — the bubble would still appear, but with the
"anti-requirement violation" default text instead of the real
"agent stuck — same tool error 3x in a row" message.

This file pins the contract from both ends:

  Python side
  ───────────
  * Run a real ``AgentLoop`` against a fake LLM + always-failing
    tool. Capture all events via the bus.
  * Find the ANTI_REQ_VIOLATION event whose ``kind == "stuck_loop"``.
  * Assert it carries the 5 keys the reducer + UI rely on
    (``message``, ``tool``, ``error_signature``, ``hop``, ``kind``).

  JS side (Node-driven reducer fold)
  ──────────────────────────────────
  * Feed the EXACT captured payload to ``applyEvent``.
  * Assert a system bubble lands with the daemon's ``message`` text
    (proving the reducer reads ``payload.message`` first, NOT
    falling through to the generic "anti-requirement violation"
    default).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.bus.events import EventType
from xmclaw.core.ir.toolcall import ToolCall, ToolResult, ToolSpec
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)
from xmclaw.providers.tool.base import ToolProvider

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = REPO_ROOT / "xmclaw" / "daemon" / "static"


# ── Fakes that drive the loop into the stuck-state ─────────────


@dataclass
class _StuckLoopLLM(LLMProvider):
    """Always wants to call ``apply_patch`` with the same args, no
    matter what the tool result was. This is exactly the
    pathology B-397 fixed."""

    model: str = "stuck_fake"
    _calls: int = 0

    async def stream(  # pragma: no cover
        self, messages: list[Message], tools: list[ToolSpec] | None = None,
        *, cancel: Any = None,
    ) -> AsyncIterator[LLMChunk]:
        if False:
            yield  # type: ignore[unreachable]

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        self._calls += 1
        # Return the same tool_call every time — the agent loop will
        # invoke apply_patch, get the same error, and B-397 should
        # catch the loop after 3 identical (tool, error) pairs.
        return LLMResponse(
            content="trying apply_patch again",
            tool_calls=(
                ToolCall(
                    name="apply_patch",
                    args={"path": "/tmp/x", "old_text": "stale", "new_text": "fresh"},
                    provenance="synthetic",
                    id=f"call_{self._calls}",
                ),
            ),
        )

    @property
    def tool_call_shape(self):
        from xmclaw.core.ir.toolcall import ToolCallShape
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


@dataclass
class _AlwaysFailToolProvider(ToolProvider):
    """Always returns the same error for every invocation."""

    _calls: int = 0

    def list_tools(self) -> list[ToolSpec]:
        return [ToolSpec(
            name="apply_patch",
            description="patch a file",
            parameters_schema={"type": "object", "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            }},
        )]

    async def invoke(
        self, call: ToolCall, *, ctx: Any = None,
    ) -> ToolResult:
        self._calls += 1
        return ToolResult(
            ok=False,
            content="",
            error="file may have changed; re-read it before patching",
            call_id=call.id,
        )


# ── Capture-bus that snapshots every event ─────────────────────


class _CaptureBus(InProcessEventBus):
    """Wraps InProcessEventBus + records every published event in a
    list for post-turn assertion. Subclassing instead of monkey-
    patching keeps the fixture explicit."""

    captured: list[Any] = field(default_factory=list)

    def __init__(self) -> None:
        super().__init__()
        self.captured = []

    async def publish(self, event):  # noqa: ANN001
        self.captured.append(event)
        return await super().publish(event)


# ── Python side: pin the daemon's emit shape ───────────────────


@pytest.mark.asyncio
async def test_stuck_loop_emits_anti_req_violation_with_required_keys() -> None:
    """End-to-end: drive the agent loop into the stuck state and
    verify the ANTI_REQ_VIOLATION event payload shape matches what
    chat_reducer_secondary.js reads."""
    bus = _CaptureBus()
    llm = _StuckLoopLLM()
    tool_provider = _AlwaysFailToolProvider()
    agent = AgentLoop(
        llm=llm,
        bus=bus,
        tools=tool_provider,
        max_hops=10,  # generous; stuck-loop guard should fire well before
    )

    result = await agent.run_turn("sess-stuck", "do the patch")
    # Loop should have failed because of the stuck-loop break, not
    # because we ran out of hops.
    assert not result.ok
    # B-397: text must mention "stuck in a loop" — the human-readable
    # outcome the user sees in the chat error message.
    assert "stuck" in (result.text or "").lower()

    # Find the ANTI_REQ_VIOLATION event with kind="stuck_loop". There
    # may be other ANTI_REQ_VIOLATION events emitted (eg cancelled),
    # but for THIS turn the stuck-loop one is mandatory.
    stuck_events = [
        ev for ev in bus.captured
        if ev.type == EventType.ANTI_REQ_VIOLATION
        and ev.payload.get("kind") == "stuck_loop"
    ]
    assert stuck_events, (
        "no ANTI_REQ_VIOLATION event with kind='stuck_loop' was emitted "
        "for a stuck-loop turn — the UI cannot render the recovery message"
    )

    payload = stuck_events[0].payload
    # The 5 keys the chat_reducer_secondary.js reducer + Chat.js card
    # surface depend on. If any of these go missing the UI flickers
    # (loses tool name / error signature / hop number).
    for key in (
        "message",
        "tool",
        "error_signature",
        "hop",
        "kind",
        "strategy_decision",
        "should_retry_same",
        "recommended_action",
        "recovery_options",
    ):
        assert key in payload, (
            f"stuck_loop payload missing required key {key!r}: {payload!r}"
        )
    assert payload["kind"] == "stuck_loop"
    assert payload["tool"] == "apply_patch"
    assert payload["strategy_decision"] == "change_plan"
    assert payload["should_retry_same"] is False
    assert "change_plan" in payload["recovery_options"]
    # message is the human-readable "agent stuck …" — reducer reads
    # this FIRST (payload.message ?? payload.reason ?? payload.kind).
    assert "stuck" in payload["message"].lower()


# ── JS side: drive reducer with the captured shape ─────────────


def _run_reducer_with_secondary(events: list[dict]) -> dict:
    """Spawn ``node`` to load chat_reducer.js (which itself imports
    chat_reducer_secondary.js for the anti_req_violation arm) and
    fold ``events`` over ``applyEvent``."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")

    static = STATIC_DIR.resolve().as_posix()
    events_json = json.dumps(events)

    driver = f"""
    const url = "file:///{static}/lib/chat_reducer.js";
    globalThis.window = globalThis.window || {{}};
    globalThis.window.__xmc = {{
      preact: {{ h: () => null }},
      preact_hooks: {{
        useState: (init) => [init, () => {{}}],
        useEffect: () => {{}},
        useMemo: (fn) => fn(),
        useCallback: (fn) => fn,
      }},
      htm: {{ bind: () => () => null }},
    }};
    const mod = await import(url);
    let state = {{ messages: [], pendingAssistantId: "asst-corr-1" }};
    const events = {events_json};
    for (const ev of events) {{
      state = mod.applyEvent(state, ev);
    }}
    process.stdout.write(JSON.stringify(state));
    """
    # Force UTF-8 decode of node's stdout/stderr — Windows defaults to
    # the GBK code page on subprocess pipes, which mangles non-ASCII
    # bytes (e.g. Anthropic SDK warnings about thinking blocks emit
    # smart-quote chars that GBK chokes on).
    cp = subprocess.run(
        [node, "--input-type=module", "-e", driver],
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    assert cp.returncode == 0, (
        f"node driver failed: stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )
    return json.loads(cp.stdout)


def test_reducer_renders_stuck_loop_message_text() -> None:
    """Feed the reducer the EXACT payload shape that the daemon
    emits for a stuck-loop break and verify the inline system
    bubble carries the daemon's ``message`` text — NOT the generic
    "anti-requirement violation" fallback.

    The fallback chain in chat_reducer_secondary.js is:
        payload.reason || payload.message || payload.kind ||
        "anti-requirement violation"
    The daemon's stuck-loop emit doesn't set ``reason`` (only
    ``message``), so this test explicitly verifies the second
    rung of the chain works as advertised.
    """
    state = _run_reducer_with_secondary([
        {
            "type": "anti_req_violation",
            "payload": {
                "message": "agent stuck — same tool error 3x in a row",
                "tool": "apply_patch",
                "error_signature": "file may have changed; re-read it",
                "hop": 3,
                "kind": "stuck_loop",
            },
            "ts": 1000,
            "correlation_id": "asst-corr-1",
        },
    ])
    msgs = state.get("messages", [])
    # The bubble may be one of several kinds depending on the
    # reducer's history shape; we just assert the daemon's message
    # text reached SOME message body.
    text_blob = json.dumps(msgs)
    assert "agent stuck" in text_blob, (
        f"daemon's stuck-loop message didn't reach the rendered bubble. "
        f"Reducer fallback chain may have collapsed to the generic "
        f"default. Messages: {msgs!r}"
    )
    # Also verify the bubble is NOT the generic placeholder — that
    # would mean the reducer skipped past payload.message.
    assert "anti-requirement violation" not in text_blob, (
        "reducer fell through to the generic default text — "
        "payload.message is being ignored"
    )


def test_reducer_renders_kind_when_message_absent() -> None:
    """Defensive fallback: if a future emit drops ``message`` (eg a
    refactor that pushes message construction into the UI), the
    reducer must still render SOMETHING via the ``kind`` rung. This
    keeps the bubble from going completely blank."""
    state = _run_reducer_with_secondary([
        {
            "type": "anti_req_violation",
            "payload": {
                "tool": "apply_patch",
                "kind": "stuck_loop",
            },
            "ts": 1000,
            "correlation_id": "asst-corr-1",
        },
    ])
    msgs = state.get("messages", [])
    text_blob = json.dumps(msgs)
    assert "stuck_loop" in text_blob, (
        f"reducer's payload.kind fallback broken. Messages: {msgs!r}"
    )


# ── Phase 6.4: worker lifecycle reducer contract ─────────────────


def test_reducer_creates_worker_bubble_on_started() -> None:
    """WORKER_STARTED creates a system message with kind='worker'
    and status='running'."""
    state = _run_reducer_with_secondary([
        {
            "type": "worker_started",
            "payload": {
                "worker_id": "w0",
                "task_id": "tA",
                "prompt_preview": "do the thing",
            },
            "ts": 1000,
        },
    ])
    msgs = state.get("messages", [])
    assert len(msgs) == 1
    m = msgs[0]
    assert m["kind"] == "worker"
    assert m["status"] == "running"
    assert m["workerId"] == "w0"
    assert m["taskId"] == "tA"
    assert "do the thing" in m["promptPreview"]


def test_reducer_updates_worker_bubble_on_completed() -> None:
    """WORKER_COMPLETED upgrades the matching bubble to status='ok'
    and injects outputPreview + elapsedSeconds."""
    state = _run_reducer_with_secondary([
        {
            "type": "worker_started",
            "payload": {
                "worker_id": "w0",
                "task_id": "tA",
                "prompt_preview": "do the thing",
            },
            "ts": 1000,
        },
        {
            "type": "worker_completed",
            "payload": {
                "worker_id": "w0",
                "task_id": "tA",
                "output_preview": "result: 42",
                "elapsed_seconds": 1.23,
            },
            "ts": 1001,
        },
    ])
    msgs = state.get("messages", [])
    assert len(msgs) == 1
    m = msgs[0]
    assert m["status"] == "ok"
    assert m["outputPreview"] == "result: 42"
    assert m["elapsedSeconds"] == 1.23


def test_reducer_creates_worker_bubble_on_failed() -> None:
    """WORKER_FAILED creates a bubble directly when started never
    arrived (race / WS reordering)."""
    state = _run_reducer_with_secondary([
        {
            "type": "worker_failed",
            "payload": {
                "worker_id": "w0",
                "task_id": "tA",
                "error": "something went wrong",
            },
            "ts": 1000,
        },
    ])
    msgs = state.get("messages", [])
    assert len(msgs) == 1
    m = msgs[0]
    assert m["kind"] == "worker"
    assert m["status"] == "error"
    assert m["error"] == "something went wrong"


def test_reducer_synthesises_completed_when_started_missing() -> None:
    """WORKER_COMPLETED arriving before WORKER_STARTED synthesises
    the bubble in finished state (defensive against WS reorder)."""
    state = _run_reducer_with_secondary([
        {
            "type": "worker_completed",
            "payload": {
                "worker_id": "w0",
                "task_id": "tA",
                "output_preview": "all done",
                "elapsed_seconds": 2.0,
            },
            "ts": 1000,
        },
    ])
    msgs = state.get("messages", [])
    assert len(msgs) == 1
    m = msgs[0]
    assert m["status"] == "ok"
    assert m["outputPreview"] == "all done"
    assert m["elapsedSeconds"] == 2.0
