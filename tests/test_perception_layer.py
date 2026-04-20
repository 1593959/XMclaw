"""Phase E1 perception-layer regression tests.

These pin three bugs that previously let bad signals propagate silently into
the evolution pipeline:

* **M01** — `turn_data["tool_observations"]` was built before tools ran and then
  orphaned when the loop rebound `observations = []`, so reflection always saw
  an empty list. Test: observations captured in turn_data must match the real
  tools that ran.
* **M29** — `TaskClassifier` swallowed failures and returned a GENERAL profile
  that was structurally indistinguishable from a genuine classification. Test:
  empty input, fast-path, and fallback paths each carry a distinct `source`.
* **M73/M74** — `ReflectionEngine.reflect()` returned `{}` for empty history AND
  for parse failures, collapsing two different signals. Test: each path now
  returns a dict with a `status` field the evolution layer can branch on.
"""
from __future__ import annotations

import pytest

import inspect

from xmclaw.core import agent_loop
from xmclaw.core.reflection import ReflectionEngine, ReflectionStatus
from xmclaw.core.task_classifier import (
    ClassifierSource,
    Complexity,
    TaskClassifier,
    TaskType,
)


# ── M01: observations reference is not orphaned ────────────────────────────

def test_agent_loop_observations_not_rebound_inside_tool_loop():
    """Pins the fix for M01: `observations` must be bound ONCE per turn so
    the reference stored in `turn_data["tool_observations"]` stays live
    after tool execution mutates the list. Earlier versions of run() did::

        observations = []          # turn_data captures this empty list
        turn_data = {..., "tool_observations": observations, ...}
        self._turn_history.append(turn_data)
        ...
        observations = []          # <-- rebinds, orphans turn_data's ref
        for call in tool_calls: observations.append(...)

    If a regression reintroduces the second `observations = []`, reflection
    silently sees empty observations again. This test fails fast.
    """
    src = inspect.getsource(agent_loop.AgentLoop.run)
    # The ONLY legal assignment to `observations` is the typed init line.
    # Any other bare `observations = ...` is the bug.
    bad = [ln for ln in src.splitlines()
           if ln.strip().startswith("observations = [")
           and "observations: list[dict] = []" not in ln]
    assert not bad, (
        "Found re-assignment(s) of `observations` inside AgentLoop.run — "
        "this orphans the reference stored in turn_data and brings back "
        f"bug M01. Offending line(s):\n  " + "\n  ".join(bad)
    )


def test_agent_loop_records_turn_data_after_tool_loop():
    """turn_history.append must happen AFTER observations is populated.
    We locate the tool-execution `for call in tool_calls:` loop and assert
    a `self._turn_history.append(...)` call appears below it in run()."""
    src = inspect.getsource(agent_loop.AgentLoop.run)
    lines = src.splitlines()
    # Find the start of the tool-execution loop (the one that APPENDS to observations)
    tool_loop_line = None
    for i, ln in enumerate(lines):
        if "for call in tool_calls:" in ln and i > 0:
            # The real tool loop is the one whose body contains observations.append.
            # Scan until dedent (next line at same-or-lesser indent than `for`).
            indent = len(ln) - len(ln.lstrip())
            body = []
            for j in range(i + 1, len(lines)):
                nxt = lines[j]
                if nxt.strip() == "":
                    body.append(nxt)
                    continue
                nxt_indent = len(nxt) - len(nxt.lstrip())
                if nxt_indent <= indent:
                    break
                body.append(nxt)
            if any("observations.append" in b for b in body):
                tool_loop_line = i
                break
    assert tool_loop_line is not None, "could not locate tool-execution loop"
    tail = "\n".join(lines[tool_loop_line:])
    assert "self._turn_history.append(" in tail, (
        "turn_history.append must appear AFTER the tool loop so observations "
        "are populated before the reflection layer sees them (bug M01)."
    )


# ── M29: classifier provenance ──────────────────────────────────────────────

class _StubLLM:
    """Minimal LLMRouter stand-in that raises on stream() — forces fallback."""

    async def stream(self, messages, **kwargs):  # noqa: D401
        raise RuntimeError("LLM intentionally broken for test")
        yield  # pragma: no cover  (make this an async generator)


@pytest.mark.asyncio
async def test_classifier_empty_input_source_is_empty():
    clf = TaskClassifier(_StubLLM())
    profile = await clf.classify("")
    assert profile["source"] == ClassifierSource.EMPTY
    assert profile["type"] == TaskType.GENERAL


@pytest.mark.asyncio
async def test_classifier_fast_path_source_is_fast():
    clf = TaskClassifier(_StubLLM())
    profile = await clf.classify("帮我写代码 fix 这个 bug")
    assert profile["source"] == ClassifierSource.FAST
    assert profile["type"] == TaskType.CODE


@pytest.mark.asyncio
async def test_classifier_llm_failure_source_is_fallback():
    """Downstream code must be able to tell 'real GENERAL' from 'broken classifier'."""
    clf = TaskClassifier(_StubLLM())
    # Phrase that avoids every fast-path keyword so we hit the LLM branch
    profile = await clf.classify("Random ambiguous phrase xyzzy")
    assert profile["source"] == ClassifierSource.FALLBACK
    assert profile["type"] == TaskType.GENERAL
    assert profile["complexity"] == Complexity.LOW


# ── M73/M74: reflection skip/failure are first-class signals ───────────────

@pytest.mark.asyncio
async def test_reflect_no_history_returns_skipped_status():
    engine = ReflectionEngine(llm_router=_StubLLM(), memory=None)
    result = await engine.reflect(agent_id="a1", history=[])
    assert result["status"] == ReflectionStatus.SKIPPED_NO_HISTORY.value
    # Previously returned {} — the empty dict collapsed with parse_failed.
    assert "reflection" not in result


@pytest.mark.asyncio
async def test_reflect_empty_signal_returns_skipped_status():
    """All turns have no assistant text, no tools, no observations — reflection
    would just hallucinate. Must skip and mark the signal."""
    engine = ReflectionEngine(llm_router=_StubLLM(), memory=None)
    history = [
        {"user": "", "assistant": "", "tool_calls": [], "tool_observations": []},
        {"user": "", "assistant": "   ", "tool_calls": [], "tool_observations": []},
    ]
    result = await engine.reflect(agent_id="a1", history=history)
    assert result["status"] == ReflectionStatus.SKIPPED_EMPTY_SIGNAL.value


@pytest.mark.asyncio
async def test_reflect_non_empty_turn_is_not_skipped():
    """A single meaningful turn must bypass the empty-signal guard so it can
    proceed to the LLM (which may still fail further down — that's fine)."""
    engine = ReflectionEngine(llm_router=_StubLLM(), memory=None)
    history = [{"user": "hi", "assistant": "hello", "tool_calls": [], "tool_observations": []}]
    result = await engine.reflect(agent_id="a1", history=history)
    # LLM stub raises → error status, NOT skipped. The point is the guard
    # didn't short-circuit a real turn.
    assert result["status"] in (
        ReflectionStatus.ERROR.value,
        ReflectionStatus.PARSE_FAILED.value,
    )
