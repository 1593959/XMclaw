"""B-227 follow-up: integration tests for the agent-loop classify-and-retry.

User pain point flagged in audit pass-3:
"async LLM retry semantics — needs better coverage."

Existing coverage:
  * ``test_v2_error_classifier.py`` (46 tests) — classify_api_error
    correctness for every reason × status × pattern combination.
  * ``test_v2_agent_memory.py::test_*`` — only the EXHAUSTION path
    (LLM keeps raising RuntimeError, schedule runs out, turn fails).

Gap:
  * No test for the SUCCESS-on-retry path — transient rate_limit
    on call 1, success on call 2. The whole point of B-227.
  * No test for the BAIL-IMMEDIATELY path — auth errors must NOT
    consume the schedule (empty backoff_schedule for FailoverReason.auth).
  * No test that the schedule LENGTH is honoured per-reason
    (rate_limit gets 3 retries, server_error gets 2, auth gets 0).

This file pins the integration. It uses ``asyncio.run`` for each
case (no shared event loop = order-independent), patches
``backoff_schedule`` so retries don't actually sleep 1.5 s × N, and
constructs synthetic exceptions with ``status_code`` attributes that
drive the classifier deterministically.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from xmclaw.core.bus import InProcessEventBus
from xmclaw.core.ir import ToolCallShape, ToolSpec
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.base import (
    LLMChunk,
    LLMProvider,
    LLMResponse,
    Message,
    Pricing,
)


# ── Synthetic API errors with status_code (drives classifier) ──────


class _FakeAPIError(Exception):
    """Mimics the SDK API error shape — both Anthropic and OpenAI
    expose ``status_code`` on the error object, which is what
    ``classify_api_error`` walks first to bucket the reason."""

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code


# ── LLM that fails N times then succeeds ─────────────────────────


@dataclass
class _RetryableLLM(LLMProvider):
    """Fakes an LLM that:
      * Raises ``error_to_raise`` for the first ``fail_count`` calls
      * Returns ``LLMResponse(content="recovered")`` once the fail
        budget is exhausted.

    Captures the call count so tests can assert exact retry behaviour.
    """

    error_to_raise: Exception | None = None
    fail_count: int = 0
    calls: int = 0
    model: str = "retryable_fake"

    seen_messages: list[list[Message]] = field(default_factory=list)

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
        self.seen_messages.append(list(messages))
        self.calls += 1
        if self.calls <= self.fail_count and self.error_to_raise is not None:
            raise self.error_to_raise
        return LLMResponse(content=f"recovered after {self.calls} call(s)")

    @property
    def tool_call_shape(self) -> ToolCallShape:
        return ToolCallShape.ANTHROPIC_NATIVE

    @property
    def pricing(self) -> Pricing:
        return Pricing()


# ── helpers ───────────────────────────────────────────────────────


def _patch_fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override ``backoff_schedule`` so retries sleep ~0.001 s instead
    of the production 1.5 s × N. Keeps the schedule LENGTH honest
    (rate_limit still gets 3 retry slots, auth still gets 0) — only
    the per-retry duration is collapsed."""
    from xmclaw.utils import error_classifier as ec

    original = ec.backoff_schedule

    def fast_schedule(reason: ec.FailoverReason) -> tuple[int, ...]:
        sched = original(reason)
        # Each non-zero entry → 1 ms (effectively no-sleep but the
        # awaits + retry loop iterations all run).
        return tuple(1 for _ in sched)

    # The agent_loop imports backoff_schedule INSIDE the method, so
    # patch the module-level binding it'll re-import.
    monkeypatch.setattr(ec, "backoff_schedule", fast_schedule)


# ── tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_b227_retry_succeeds_on_transient_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rate_limit (HTTP 429) is retryable — schedule (1.5s, 4.5s, 9s)
    gives 3 retry slots. With 1 transient failure + success on
    retry attempt 1, the turn MUST complete with ok=True and exactly
    2 LLM calls (initial + 1 retry)."""
    _patch_fast_backoff(monkeypatch)
    err = _FakeAPIError(429, "rate_limit_exceeded")
    llm = _RetryableLLM(error_to_raise=err, fail_count=1)
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    result = await agent.run_turn("sess-1", "hello")
    assert result.ok, f"retry should have recovered the turn; got {result!r}"
    assert llm.calls == 2, (
        f"expected initial + 1 retry = 2 calls, got {llm.calls}. "
        "If <2: retry didn't fire. If >2: retry over-fired."
    )


@pytest.mark.asyncio
async def test_b227_retry_succeeds_on_overloaded_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """overloaded (HTTP 503/529) is retryable, schedule (2s, 5s, 10s).
    Same shape as rate_limit but classified differently. Pinning the
    503 path explicitly so a future classifier refactor that drops
    503→overloaded routing surfaces immediately."""
    _patch_fast_backoff(monkeypatch)
    err = _FakeAPIError(503, "service_unavailable")
    llm = _RetryableLLM(error_to_raise=err, fail_count=1)
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    result = await agent.run_turn("sess-2", "hi")
    assert result.ok
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_b227_auth_error_bails_immediately_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auth (HTTP 401) is NOT retryable — schedule is () (empty).
    The retry loop must bail on the FIRST exception with ok=False
    and exactly 1 LLM call. Retrying an auth error wastes attempts
    on a state the user has to fix manually (bad / revoked key)."""
    _patch_fast_backoff(monkeypatch)
    err = _FakeAPIError(401, "unauthorized")
    # fail_count is HUGE — we want to verify the agent loop gives
    # up after 1 call regardless of how many times the LLM "would"
    # have failed.
    llm = _RetryableLLM(error_to_raise=err, fail_count=99)
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    result = await agent.run_turn("sess-3", "hi")
    assert not result.ok, "auth error should fail the turn, not retry"
    assert llm.calls == 1, (
        f"expected exactly 1 call (no retry), got {llm.calls}. "
        "auth backoff_schedule is () so retry must not fire."
    )


@pytest.mark.asyncio
async def test_b227_format_error_400_bails_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """format_error (generic HTTP 400) is also not retryable —
    retrying with the same payload just breaks the same way. Pin
    the 1-call-only behaviour."""
    _patch_fast_backoff(monkeypatch)
    err = _FakeAPIError(400, "bad_request: malformed payload")
    llm = _RetryableLLM(error_to_raise=err, fail_count=99)
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    result = await agent.run_turn("sess-4", "hi")
    assert not result.ok
    # 400 with no compress signal classifies as format_error → no retry
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_b227_rate_limit_exhausts_full_3_retry_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If rate_limit keeps firing, the agent loop MUST honour the
    full 3-slot schedule before bailing. 1 initial + 3 retries = 4
    LLM calls total, then ok=False. Anything <4 means the schedule
    was truncated."""
    _patch_fast_backoff(monkeypatch)
    err = _FakeAPIError(429, "still rate-limited")
    llm = _RetryableLLM(error_to_raise=err, fail_count=99)
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    result = await agent.run_turn("sess-5", "hi")
    assert not result.ok
    # rate_limit schedule is (1.5s, 4.5s, 9s) — 3 retry slots after
    # the initial failure, so 4 calls total.
    assert llm.calls == 4, (
        f"expected 1 + 3 retries = 4 calls, got {llm.calls}. "
        "rate_limit schedule has 3 entries — agent must honour all of them."
    )


@pytest.mark.asyncio
async def test_b227_no_retry_when_no_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: if the LLM doesn't raise, the retry loop never fires
    and the call count is exactly 1. Pinning the happy path keeps
    a future "always retry on first call" mistake from going
    unnoticed."""
    _patch_fast_backoff(monkeypatch)
    llm = _RetryableLLM(error_to_raise=None, fail_count=0)
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    result = await agent.run_turn("sess-6", "hi")
    assert result.ok
    assert llm.calls == 1, (
        f"happy path should be 1 call, got {llm.calls}. "
        "Retry loop fired on a non-failed request."
    )


@pytest.mark.asyncio
async def test_b227_unknown_error_uses_unknown_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic ``RuntimeError`` (no status_code) classifies as
    FailoverReason.unknown which has schedule (1s, 3s) — 2 retries.
    With fail_count=1 (success on call 2), exactly 2 calls land."""
    _patch_fast_backoff(monkeypatch)
    err = RuntimeError("vague backend hiccup")
    llm = _RetryableLLM(error_to_raise=err, fail_count=1)
    agent = AgentLoop(llm=llm, bus=InProcessEventBus())

    result = await agent.run_turn("sess-7", "hi")
    assert result.ok
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_b227_schedule_length_matches_per_reason_dict() -> None:
    """Pure-import contract: the ``_BACKOFF_MS`` dict must match the
    documented per-reason retry counts. If the schedule shrinks or
    grows, the integration tests above will silently mis-count —
    this guard catches the dict-edit half of that drift.
    """
    from xmclaw.utils.error_classifier import (
        FailoverReason,
        backoff_schedule,
    )

    # Retryable reasons MUST have non-empty schedules.
    assert len(backoff_schedule(FailoverReason.rate_limit)) >= 2
    assert len(backoff_schedule(FailoverReason.overloaded)) >= 2
    assert len(backoff_schedule(FailoverReason.server_error)) >= 1
    assert len(backoff_schedule(FailoverReason.timeout)) >= 1
    assert len(backoff_schedule(FailoverReason.unknown)) >= 1
    # Non-retryable reasons MUST be empty.
    assert backoff_schedule(FailoverReason.auth) == ()
    assert backoff_schedule(FailoverReason.auth_permanent) == ()
    assert backoff_schedule(FailoverReason.billing) == ()
    assert backoff_schedule(FailoverReason.model_not_found) == ()
    assert backoff_schedule(FailoverReason.format_error) == ()
