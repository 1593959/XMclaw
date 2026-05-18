"""Plan-mode tools — Wave-32+ (2026-05-18).

Verifies:
  * ``enter_plan_mode`` + ``exit_plan_mode`` are advertised by
    BuiltinTools' list_tools()
  * ``enter_plan_mode`` flips the gate ON for the current session
  * Mutating tools (file_write, bash, etc.) refuse cleanly while
    plan mode is active; read-only tools (file_read, glob_files)
    keep working
  * ``exit_plan_mode`` flips the gate OFF and returns the plan
  * ``set_plan_mode`` (used by the WS handler for the frontend
    Plan/Act toggle) flips the gate without going through a tool call
  * Outside a session contextvar the gate is open (CLI / test path)
"""
from __future__ import annotations

import pytest

from xmclaw.core.agent_context import use_current_session_id
from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool.builtin import BuiltinTools
from xmclaw.providers.tool.builtin_planmode import (
    PLAN_MODE_BLOCKED_TOOLS,
    clear_plan_mode_sessions,
    is_blocked_by_plan_mode,
    is_session_in_plan_mode,
    set_plan_mode,
)


@pytest.fixture(autouse=True)
def _clean_plan_mode():
    """Wipe the process-level plan-mode set between tests so a
    leaked flag from one test doesn't pollute the next."""
    clear_plan_mode_sessions()
    yield
    clear_plan_mode_sessions()


def _call(name: str, **args: object) -> ToolCall:
    return ToolCall(name=name, args=args, provenance="synthetic")


def test_plan_mode_specs_advertised() -> None:
    """Both new specs must surface from list_tools(). Test-time
    construction has no providers wired, so this also asserts the
    tools are gating-free (always-advertised)."""
    tools = BuiltinTools()
    names = {s.name for s in tools.list_tools()}
    assert "enter_plan_mode" in names
    assert "exit_plan_mode" in names


@pytest.mark.asyncio
async def test_enter_plan_mode_outside_session_errors() -> None:
    """Without a session contextvar the tool can't flip any session's
    flag — return a clear error rather than silently succeeding."""
    tools = BuiltinTools()
    res = await tools.invoke(_call("enter_plan_mode"))
    assert res.ok is False
    assert "session_id" in (res.error or "").lower()


@pytest.mark.asyncio
async def test_enter_then_exit_plan_mode_flips_gate() -> None:
    """Happy path: enter sets the flag, exit clears it, plan text is
    echoed back to the LLM in the exit result so the agent can
    reference what it submitted."""
    tools = BuiltinTools()
    with use_current_session_id("sess-1"):
        enter = await tools.invoke(_call("enter_plan_mode"))
        assert enter.ok, enter.error
        assert "Entered plan mode" in enter.content
        assert is_session_in_plan_mode("sess-1")

        exit_res = await tools.invoke(
            _call("exit_plan_mode", plan="1. read file_a\n2. edit foo()"),
        )
        assert exit_res.ok, exit_res.error
        assert "1. read file_a" in exit_res.content
        assert not is_session_in_plan_mode("sess-1")


@pytest.mark.asyncio
async def test_double_enter_is_idempotent() -> None:
    tools = BuiltinTools()
    with use_current_session_id("sess-2"):
        first = await tools.invoke(_call("enter_plan_mode"))
        assert first.ok
        second = await tools.invoke(_call("enter_plan_mode"))
        assert second.ok
        assert "Already in plan mode" in second.content


@pytest.mark.asyncio
async def test_exit_without_plan_arg_errors() -> None:
    """The whole point of exit is to surface the plan text — refuse
    when the agent skipped it. Otherwise the user would see an empty
    plan and be left guessing."""
    tools = BuiltinTools()
    with use_current_session_id("sess-3"):
        await tools.invoke(_call("enter_plan_mode"))
        res = await tools.invoke(_call("exit_plan_mode"))
        assert res.ok is False
        assert "plan" in (res.error or "").lower()


@pytest.mark.asyncio
async def test_exit_when_not_in_mode_is_lenient() -> None:
    """Calling exit without first entering should NOT error — agent
    may have lost track of mode state."""
    tools = BuiltinTools()
    with use_current_session_id("sess-4"):
        res = await tools.invoke(_call("exit_plan_mode", plan="(empty)"))
        assert res.ok
        assert "no-op" in res.content.lower()


def test_is_blocked_by_plan_mode_for_each_blocked_tool() -> None:
    """Every name in PLAN_MODE_BLOCKED_TOOLS must report blocked when
    the current session is in plan mode. Lock this down so future
    additions to the set don't silently no-op due to a missing
    entry in the gate check."""
    set_plan_mode("sess-block", True)
    with use_current_session_id("sess-block"):
        for tool_name in PLAN_MODE_BLOCKED_TOOLS:
            assert is_blocked_by_plan_mode(tool_name), tool_name


def test_read_only_tools_not_blocked() -> None:
    """The whole point of plan mode is to encourage exploration —
    pin a representative set of read-only tools to the open path."""
    set_plan_mode("sess-read", True)
    with use_current_session_id("sess-read"):
        for tool_name in [
            "file_read", "glob_files", "grep_files",
            "web_search", "web_fetch", "ask_user_question",
            "list_dir",
        ]:
            assert not is_blocked_by_plan_mode(tool_name), tool_name


def test_gate_open_outside_session_contextvar() -> None:
    """CLI / test contexts that don't run through the WS handler
    don't have a session contextvar — the gate must be open so
    tooling that exercises BuiltinTools directly isn't accidentally
    locked out."""
    set_plan_mode("sess-x", True)  # some other session in plan mode
    # No use_current_session_id wrapping → contextvar is None.
    assert not is_blocked_by_plan_mode("bash")
    assert not is_blocked_by_plan_mode("file_write")


@pytest.mark.asyncio
async def test_invoke_blocks_mutating_tool_with_clear_error() -> None:
    """End-to-end at the BuiltinTools.invoke() boundary: a blocked
    tool returns ok=False with a 'plan mode' error message, not a
    traceback."""
    tools = BuiltinTools()
    with use_current_session_id("sess-end2end"):
        await tools.invoke(_call("enter_plan_mode"))
        # Pick a mutating tool that BuiltinTools dispatches to.
        # file_write hits a sandbox check first if no allowed_dirs is
        # set; the plan-mode gate sits ABOVE that, so we get the
        # plan-mode error, not a sandbox error.
        res = await tools.invoke(
            _call("file_write", path="/tmp/foo", content="hi"),
        )
        assert res.ok is False
        assert "plan mode" in (res.error or "").lower()


def test_set_plan_mode_idempotent_and_clear() -> None:
    """The WS-level setter (used when the frontend Plan chip is
    toggled) must be idempotent + reversible. Double-set is a no-op,
    set then clear empties the gate."""
    set_plan_mode("sess-A", True)
    set_plan_mode("sess-A", True)
    assert is_session_in_plan_mode("sess-A")
    set_plan_mode("sess-A", False)
    assert not is_session_in_plan_mode("sess-A")
    # Clearing a non-existent session is also a no-op (no KeyError).
    set_plan_mode("never-set", False)
