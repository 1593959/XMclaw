"""B-92: ask_user_question tool — agent stops mid-turn for user input.

Pins:
  * tool spec is advertised on every BuiltinTools instance
  * resolving a pending question via resolve_pending_question() unblocks
    the awaiting tool handler with the supplied answer
  * resolving an unknown / already-resolved question id returns False
  * missing 'question' / empty 'options' fail validation
"""
from __future__ import annotations

import asyncio

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import (
    BuiltinTools,
    _PENDING_QUESTIONS,
    resolve_pending_question,
)


@pytest.fixture(autouse=True)
def _clear_pending() -> None:
    """Don't let pending entries leak across tests."""
    _PENDING_QUESTIONS.clear()
    yield
    _PENDING_QUESTIONS.clear()


def test_spec_is_advertised() -> None:
    tools = BuiltinTools()
    names = [s.name for s in tools.list_tools()]
    assert "ask_user_question" in names


def test_resolve_unknown_question_returns_false() -> None:
    assert resolve_pending_question("nonexistent-id", "anything") is False


@pytest.mark.asyncio
async def test_tool_unblocks_when_question_resolved() -> None:
    tools = BuiltinTools()
    call = ToolCall(
        id="c1", provenance="synthetic",
        name="ask_user_question",
        args={
            "question": "Pick one",
            "options": [
                {"label": "A", "value": "alpha"},
                {"label": "B", "value": "beta"},
            ],
        },
    )

    invoke_task = asyncio.create_task(tools.invoke(call))

    # Wait until the future is registered. The handler does the publish
    # synchronously then awaits — so a small grace yield gets us there.
    for _ in range(10):
        await asyncio.sleep(0.01)
        if _PENDING_QUESTIONS:
            break
    assert _PENDING_QUESTIONS, "tool handler should have registered a pending future"

    qid = next(iter(_PENDING_QUESTIONS.keys()))
    assert resolve_pending_question(qid, "alpha") is True

    result = await invoke_task
    assert result.ok is True
    assert result.content == "alpha"
    # Resolved entry must be cleared from the dict.
    assert qid not in _PENDING_QUESTIONS


@pytest.mark.asyncio
async def test_multi_select_answer_returned_as_csv() -> None:
    tools = BuiltinTools()
    call = ToolCall(
        id="c1", provenance="synthetic",
        name="ask_user_question",
        args={
            "question": "Pick all that apply",
            "options": [
                {"label": "A", "value": "a"},
                {"label": "B", "value": "b"},
                {"label": "C", "value": "c"},
            ],
            "multi_select": True,
        },
    )
    invoke_task = asyncio.create_task(tools.invoke(call))
    for _ in range(10):
        await asyncio.sleep(0.01)
        if _PENDING_QUESTIONS:
            break
    qid = next(iter(_PENDING_QUESTIONS.keys()))
    resolve_pending_question(qid, ["a", "c"])
    result = await invoke_task
    assert result.ok is True
    # Plain comma-separated string for the LLM.
    assert "a" in result.content and "c" in result.content


@pytest.mark.asyncio
async def test_missing_question_fails() -> None:
    tools = BuiltinTools()
    call = ToolCall(
        id="c1", provenance="synthetic", name="ask_user_question",
        args={"options": [{"label": "x", "value": "x"}]},
    )
    result = await tools.invoke(call)
    assert result.ok is False
    assert "question" in (result.error or "")


@pytest.mark.asyncio
async def test_empty_options_fail() -> None:
    tools = BuiltinTools()
    call = ToolCall(
        id="c1", provenance="synthetic", name="ask_user_question",
        args={"question": "?", "options": []},
    )
    result = await tools.invoke(call)
    assert result.ok is False
    assert "options" in (result.error or "")


@pytest.mark.asyncio
async def test_resolve_after_resolve_is_idempotent() -> None:
    """Double-clicking an option in the UI shouldn't crash anything;
    second resolve returns False."""
    tools = BuiltinTools()
    call = ToolCall(
        id="c1", provenance="synthetic", name="ask_user_question",
        args={
            "question": "?",
            "options": [{"label": "x", "value": "x"}],
        },
    )
    invoke_task = asyncio.create_task(tools.invoke(call))
    for _ in range(10):
        await asyncio.sleep(0.01)
        if _PENDING_QUESTIONS:
            break
    qid = next(iter(_PENDING_QUESTIONS.keys()))
    assert resolve_pending_question(qid, "x") is True
    # The first resolve removed the entry — second call sees nothing.
    assert resolve_pending_question(qid, "y") is False
    result = await invoke_task
    # The agent gets the FIRST answer (x), not the stale double-click.
    assert result.content == "x"
