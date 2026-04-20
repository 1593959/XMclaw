"""Regression tests for ReflectionEngine.

The primary bug this file pins down:

  Every reflection call returned ``{"status": "parse_failed"}`` because
  the engine did::

      response = ""
      async for chunk in self.llm.stream(messages):
          response += chunk

  But ``llm.stream()`` yields **JSON event envelopes** like
  ``{"type":"text","content":"…"}`` — not raw text. Concatenating them
  produces a blob like
  ``{"type":"text","content":"{"}{"type":"text","content":"\"sum\""}…``
  which ``_extract_json`` can never recover JSON from, so every
  reflection silently collapsed into ``parse_failed``. The UI showed
  this as "反思失败" on the right rail and 思考="parse_failed".

  Fix: call ``self.llm.complete(messages)`` — that returns the raw
  model text. The tests here pin:

  1. Reflection uses ``.complete()`` and NOT ``.stream()`` (hard-fails
     if someone reintroduces the envelope-concat pattern).
  2. When ``.complete()`` returns well-formed JSON, the engine emits
     ``status == "ok"``.
  3. When ``.complete()`` returns something truly unparseable, the
     engine emits ``status == "parse_failed"`` (not OK) — so we're
     actually distinguishing the two cases, not accidentally always
     returning one.
"""
from __future__ import annotations

import pytest

from xmclaw.core.reflection import ReflectionEngine, ReflectionStatus


class _FakeMemory:
    """Minimal MemoryManager stub — reflection only needs .sqlite and
    save_insight/add_memory hooks. save_insight is called after a
    successful parse; add_memory is awaited. We capture them but do
    nothing else."""

    def __init__(self) -> None:
        self.sqlite = None
        self.insights: list[dict] = []
        self.memories: list[tuple[str, str, str]] = []

    def save_insight(self, agent_id: str, insight: dict) -> None:
        self.insights.append(insight)

    async def add_memory(self, agent_id: str, content: str, source: str = "") -> None:
        self.memories.append((agent_id, content, source))


class _CompleteLLM:
    """LLM stub that implements the CORRECT contract: ``.complete()``
    returns raw model text. ``.stream()`` is a booby-trap so any
    regression to the old envelope-concat pattern fails loudly."""

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.complete_calls = 0

    async def complete(self, messages):
        self.complete_calls += 1
        return self.response_text

    async def stream(self, messages):  # pragma: no cover
        raise AssertionError(
            "ReflectionEngine must use .complete() — .stream() yields JSON "
            "event envelopes, not raw text. See tests/test_reflection.py."
        )
        yield ""  # unreachable, keeps this a valid async generator


@pytest.mark.asyncio
async def test_reflect_uses_completion_not_event_stream(monkeypatch):
    """The exact bug: reflection must not iterate llm.stream() chunks
    as if they were text. If it does, .stream() fires its AssertionError
    and this test fails."""
    # Short-circuit AutoImprover so the test doesn't hit a real evolution
    # pipeline or the filesystem for generated artifacts.
    from xmclaw.core import reflection as reflection_mod

    class _NoopImprover:
        async def improve_from_reflection(self, agent_id, result):
            return {"status": "skipped", "reason": "test"}

    monkeypatch.setattr(reflection_mod, "AutoImprover", _NoopImprover)

    llm = _CompleteLLM(
        '{"success": true, "summary": "quick lesson", '
        '"problems": [], "lessons": ["l1"], "improvements": []}'
    )
    engine = ReflectionEngine(llm, _FakeMemory())  # type: ignore[arg-type]
    result = await engine.reflect(
        "agent-x",
        [{"user": "hi", "assistant": "hello", "tool_calls": []}],
    )
    assert llm.complete_calls == 1, ".complete() must be invoked exactly once"
    assert result["status"] == ReflectionStatus.OK.value
    assert result["reflection"]["summary"] == "quick lesson"


@pytest.mark.asyncio
async def test_reflect_parse_failed_only_when_truly_unparseable(monkeypatch):
    """If the model returns something with no JSON, we must surface
    parse_failed. This guards against the inverse regression: always
    returning OK because we stopped running _extract_json."""
    from xmclaw.core import reflection as reflection_mod

    class _NoopImprover:
        async def improve_from_reflection(self, agent_id, result):
            return {"status": "skipped", "reason": "test"}

    monkeypatch.setattr(reflection_mod, "AutoImprover", _NoopImprover)

    llm = _CompleteLLM("this response has no json at all, just prose.")
    engine = ReflectionEngine(llm, _FakeMemory())  # type: ignore[arg-type]
    result = await engine.reflect(
        "agent-x",
        [{"user": "hi", "assistant": "hello", "tool_calls": []}],
    )
    assert result["status"] == ReflectionStatus.PARSE_FAILED.value
    assert "raw" in result


@pytest.mark.asyncio
async def test_reflect_skipped_paths_dont_hit_llm():
    """Skipped paths (no history, empty signal) must short-circuit
    before touching the LLM. If someone breaks the early-return guards,
    _CompleteLLM.complete_calls catches it."""
    llm = _CompleteLLM("{}")
    engine = ReflectionEngine(llm, _FakeMemory())  # type: ignore[arg-type]

    result = await engine.reflect("agent-x", [])
    assert result["status"] == ReflectionStatus.SKIPPED_NO_HISTORY.value
    assert llm.complete_calls == 0

    empty_turn = [{"user": "hi", "assistant": "", "tool_calls": [], "tool_observations": []}]
    result = await engine.reflect("agent-x", empty_turn)
    assert result["status"] == ReflectionStatus.SKIPPED_EMPTY_SIGNAL.value
    assert llm.complete_calls == 0
