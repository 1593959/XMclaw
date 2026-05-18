"""Hook engine — unit tests for the core orchestration.

Runner-specific tests live alongside (test_v2_hook_runners.py) so
this file stays focused on the dispatch + match + merge logic.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from xmclaw.core.hooks import (
    HookContext,
    HookEngine,
    HookEvent,
    HookResult,
    HookSpec,
    build_hook_engine_from_config,
    mark_workspace_trusted,
    merge_decisions,
    parse_event,
    workspace_trust_level,
)


# ── HookEvent enum ─────────────────────────────────────────────────


def test_parse_event_pascal_case() -> None:
    assert parse_event("UserPromptSubmit") == HookEvent.USER_PROMPT_SUBMIT


def test_parse_event_snake_case_falls_through() -> None:
    assert parse_event("user_prompt_submit") == HookEvent.USER_PROMPT_SUBMIT
    assert parse_event("pre-tool-use") == HookEvent.PRE_TOOL_USE


def test_parse_event_unknown_returns_none() -> None:
    assert parse_event("DefinitelyNotAnEvent") is None
    assert parse_event("") is None


# ── merge_decisions priority ──────────────────────────────────────


def test_merge_decisions_deny_wins() -> None:
    results = [
        HookResult.allow(),
        HookResult.deny("nope"),
        HookResult.ask("are you sure?"),
    ]
    assert merge_decisions(results) == "deny"


def test_merge_decisions_ask_beats_allow() -> None:
    results = [HookResult.allow(), HookResult.ask("?")]
    assert merge_decisions(results) == "ask"


def test_merge_decisions_no_votes_returns_none() -> None:
    results = [HookResult(), HookResult(output="just a note")]
    assert merge_decisions(results) is None


# ── workspace trust marker ────────────────────────────────────────


def test_trust_level_missing_marker_untrusted(tmp_path: Path) -> None:
    assert workspace_trust_level(tmp_path) == "untrusted"


def test_trust_level_with_marker_trusted(tmp_path: Path) -> None:
    mark_workspace_trusted(tmp_path)
    assert workspace_trust_level(tmp_path) == "trusted"


def test_trust_level_none_workspace_is_untrusted() -> None:
    assert workspace_trust_level(None) == "untrusted"


# ── matcher logic ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_no_matching_hooks_returns_empty_outcome() -> None:
    engine = HookEngine()
    outcome = await engine.dispatch(HookEvent.PRE_TOOL_USE)
    assert outcome.fired_count == 0
    assert outcome.decision is None
    assert outcome.continue_ is True


@pytest.mark.asyncio
async def test_matcher_exact_string_value(tmp_path: Path) -> None:
    """Hook with ``matchers: {"tool_name": "bash"}`` only fires on
    bash tool calls."""
    mark_workspace_trusted(tmp_path)
    engine = HookEngine(workspace_root=str(tmp_path))
    # Register a function hook gated on tool_name=bash.
    sys.modules.setdefault("_test_hook_target", _make_hook_module())
    engine.register(HookSpec(
        id="bash-deny", event=HookEvent.PRE_TOOL_USE.value,
        runner="function", timeout_s=2.0,
        matchers={"tool_name": "bash"},
        config={"entry": "_test_hook_target:deny_always"},
    ))
    # Wrong tool → no fire.
    o = await engine.dispatch(
        HookEvent.PRE_TOOL_USE,
        payload={"tool_name": "file_read"},
    )
    assert o.fired_count == 0
    # Right tool → fires, decision propagates.
    o = await engine.dispatch(
        HookEvent.PRE_TOOL_USE,
        payload={"tool_name": "bash"},
    )
    assert o.fired_count == 1
    assert o.decision == "deny"


@pytest.mark.asyncio
async def test_matcher_list_value_any_of(tmp_path: Path) -> None:
    """matchers value can be a list — matches if payload is in the list."""
    mark_workspace_trusted(tmp_path)
    sys.modules.setdefault("_test_hook_target", _make_hook_module())
    engine = HookEngine(workspace_root=str(tmp_path))
    engine.register(HookSpec(
        id="fs-watcher", event=HookEvent.PRE_TOOL_USE.value,
        runner="function",
        matchers={"tool_name": ["bash", "file_write", "file_delete"]},
        config={"entry": "_test_hook_target:noop"},
    ))
    for t in ("bash", "file_write", "file_delete"):
        o = await engine.dispatch(
            HookEvent.PRE_TOOL_USE, payload={"tool_name": t},
        )
        assert o.fired_count == 1, f"{t!r} should match"
    for t in ("file_read", "glob_files"):
        o = await engine.dispatch(
            HookEvent.PRE_TOOL_USE, payload={"tool_name": t},
        )
        assert o.fired_count == 0, f"{t!r} should NOT match"


# ── function runner end-to-end ────────────────────────────────────


@pytest.mark.asyncio
async def test_function_hook_returns_deny_blocks_pipeline(tmp_path: Path) -> None:
    """A function hook that returns deny → outcome.decision='deny' +
    continue_=False."""
    mark_workspace_trusted(tmp_path)
    sys.modules.setdefault("_test_hook_target", _make_hook_module())
    engine = HookEngine(workspace_root=str(tmp_path))
    engine.register(HookSpec(
        id="block-all", event=HookEvent.PRE_TOOL_USE.value,
        runner="function",
        config={"entry": "_test_hook_target:deny_with_reason"},
    ))
    o = await engine.dispatch(
        HookEvent.PRE_TOOL_USE, payload={"tool_name": "anything"},
    )
    assert o.decision == "deny"
    assert o.continue_ is False
    assert "test-block-reason" in o.block_reason


@pytest.mark.asyncio
async def test_function_hook_returns_dict_parsed_as_result(tmp_path: Path) -> None:
    """Functions can return dict matching the JSON protocol."""
    mark_workspace_trusted(tmp_path)
    sys.modules.setdefault("_test_hook_target", _make_hook_module())
    engine = HookEngine(workspace_root=str(tmp_path))
    engine.register(HookSpec(
        id="json-out", event=HookEvent.PRE_LLM.value,
        runner="function",
        config={"entry": "_test_hook_target:return_dict_with_system_msg"},
    ))
    o = await engine.dispatch(HookEvent.PRE_LLM)
    assert "hook injected" in (o.system_messages[0] if o.system_messages else "")


@pytest.mark.asyncio
async def test_function_hook_async_callable_awaited(tmp_path: Path) -> None:
    mark_workspace_trusted(tmp_path)
    sys.modules.setdefault("_test_hook_target", _make_hook_module())
    engine = HookEngine(workspace_root=str(tmp_path))
    engine.register(HookSpec(
        id="async-fn", event=HookEvent.STOP.value,
        runner="function",
        config={"entry": "_test_hook_target:async_allow"},
    ))
    o = await engine.dispatch(HookEvent.STOP)
    assert o.fired_count == 1
    assert o.decision == "allow"


# ── workspace trust enforcement ───────────────────────────────────


@pytest.mark.asyncio
async def test_command_hook_refuses_when_untrusted(tmp_path: Path) -> None:
    """No trust marker → command hook is skipped with note, doesn't
    block the pipeline."""
    # Deliberately DON'T mark trusted.
    engine = HookEngine(workspace_root=str(tmp_path))
    engine.register(HookSpec(
        id="cmd", event=HookEvent.SESSION_START.value,
        runner="command",
        config={"command": "echo should-not-run"},
    ))
    o = await engine.dispatch(HookEvent.SESSION_START)
    assert o.continue_ is True  # skip ≠ block
    assert any("not trusted" in s for s in o.outputs)


# ── config loading ────────────────────────────────────────────────


def test_build_engine_from_config_skips_unknown_event() -> None:
    cfg = {
        "hooks": [
            {"id": "good", "event": "PreToolUse", "runner": "command",
             "command": "echo ok"},
            {"id": "typo", "event": "PreToolYouse", "runner": "command",
             "command": "echo whatever"},
        ],
    }
    engine = build_hook_engine_from_config(cfg)
    ids = {s.id for s in engine.specs()}
    assert "good" in ids
    assert "typo" not in ids


def test_build_engine_from_config_skips_unknown_runner() -> None:
    cfg = {
        "hooks": [
            {"id": "weird", "event": "Stop", "runner": "telepathy"},
        ],
    }
    engine = build_hook_engine_from_config(cfg)
    assert engine.specs() == []


def test_build_engine_from_empty_config_no_hooks() -> None:
    assert build_hook_engine_from_config(None).specs() == []
    assert build_hook_engine_from_config({}).specs() == []
    assert build_hook_engine_from_config({"hooks": []}).specs() == []


def test_register_replaces_same_id_event() -> None:
    """register() with the same (id, event) replaces, not duplicates."""
    engine = HookEngine()
    engine.register(HookSpec(
        id="x", event="Stop", runner="command",
        config={"command": "echo first"},
    ))
    engine.register(HookSpec(
        id="x", event="Stop", runner="command",
        config={"command": "echo second"},
    ))
    specs = engine.specs()
    assert len(specs) == 1
    assert specs[0].config["command"] == "echo second"


# ── HookResult JSON protocol ──────────────────────────────────────


def test_parse_result_json_camel_case_keys() -> None:
    """Claude Code uses camelCase (systemMessage, updatedInput); we
    accept both shapes."""
    from xmclaw.core.hooks.runners import _BaseRunner
    raw = json.dumps({
        "continue": True,
        "decision": "deny",
        "systemMessage": "be terse",
        "updatedInput": {"path": "fixed"},
        "reason": "redacted",
    })
    r = _BaseRunner._parse_result_json(raw, "hid")
    assert r.continue_ is True
    assert r.decision == "deny"
    assert r.system_message == "be terse"
    assert r.updated_input == {"path": "fixed"}
    assert r.reason == "redacted"
    assert r.hook_id == "hid"


def test_parse_result_json_non_json_treated_as_text() -> None:
    """Plain-text stdout from a debug ``echo hello`` style hook
    surfaces as the output field, no decision."""
    from xmclaw.core.hooks.runners import _BaseRunner
    r = _BaseRunner._parse_result_json("hello world\n", "h")
    assert r.output == "hello world"
    assert r.decision is None


# ── helper: in-memory test target module ──────────────────────────


def _make_hook_module():
    """Build a synthetic module exposing test hook functions. Avoids
    polluting the on-disk tree with a fixture file."""
    import types
    mod = types.ModuleType("_test_hook_target")

    def noop(ctx: HookContext) -> HookResult:
        return HookResult()

    def deny_always(ctx: HookContext) -> HookResult:
        return HookResult.deny("denied")

    def deny_with_reason(ctx: HookContext) -> HookResult:
        return HookResult(
            continue_=False, decision="deny",
            reason="test-block-reason",
        )

    def return_dict_with_system_msg(ctx: HookContext) -> dict:
        return {"systemMessage": "hook injected this"}

    async def async_allow(ctx: HookContext) -> HookResult:
        return HookResult.allow()

    mod.noop = noop
    mod.deny_always = deny_always
    mod.deny_with_reason = deny_with_reason
    mod.return_dict_with_system_msg = return_dict_with_system_msg
    mod.async_allow = async_allow
    return mod
