"""Unit tests for ErrorAwareRetryProvider (Batch B.2)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from xmclaw.core.ir import ToolCall, ToolResult, ToolSpec
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.providers.tool.retry_aware import (
    ErrorAwareRetryProvider,
    _is_transient,
    _parse_fixup_json,
)


# ── Stubs ────────────────────────────────────────────────────────


class _StubInner(ToolProvider):
    """Programmable inner provider."""

    def __init__(self, results_by_call: dict[tuple[str, str], ToolResult]):
        # key: (tool_name, json-sorted-args) → result
        self.results = results_by_call
        self.invoke_log: list[tuple[str, dict]] = []
        self.specs = [
            ToolSpec(
                name="file_read",
                description="Read a file from disk",
                parameters_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            ),
            ToolSpec(
                name="file_read_safe",
                description="Read a file with bounds checking",
                parameters_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            ),
        ]

    def list_tools(self):
        return self.specs

    async def invoke(self, call: ToolCall) -> ToolResult:
        self.invoke_log.append((call.name, dict(call.args)))
        key = (call.name, json.dumps(call.args, sort_keys=True))
        if key in self.results:
            return self.results[key]
        return ToolResult(
            call_id=call.id, ok=False, content=None,
            error=f"no stub for {key}",
        )


class _StubLLM:
    def __init__(self, response: str = "", raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.calls = 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        if self.raises:
            raise self.raises

        class _R:
            content = self.response  # type: ignore[attr-defined]
        return _R()


def _call(name="file_read", args=None):
    return ToolCall(
        id=f"t-{name}",
        name=name,
        args=args or {"path": "/missing.txt"},
        provenance="synthetic",
    )


def _ok(content="ok"):
    return ToolResult(call_id="t", ok=True, content=content, error=None)


def _fail(error="fail"):
    return ToolResult(call_id="t", ok=False, content=None, error=error)


# ── Transient classifier ─────────────────────────────────────────


@pytest.mark.parametrize("err,expected", [
    ("Connection refused", True),
    ("503 Service Unavailable", True),
    ("ECONNRESET", True),
    ("FileNotFoundError: /etc/foo", False),
    ("Permission denied", False),
    ("", False),
])
def test_is_transient(err, expected):
    assert _is_transient(err) is expected


# ── Disable / passthrough cases ──────────────────────────────────


async def test_passthrough_on_success():
    inner = _StubInner({("file_read", '{"path": "/x"}'): _ok("yo")})
    llm = _StubLLM(response="...")
    wrap = ErrorAwareRetryProvider(inner, llm=llm)
    r = await wrap.invoke(_call("file_read", {"path": "/x"}))
    assert r.ok is True
    # LLM never consulted on success path
    assert llm.calls == 0


async def test_passthrough_when_disabled():
    inner = _StubInner({})  # → fail
    llm = _StubLLM(response='{"action":"skip","reason":"x"}')
    wrap = ErrorAwareRetryProvider(inner, llm=llm, enabled=False)
    r = await wrap.invoke(_call())
    assert r.ok is False
    assert llm.calls == 0


async def test_passthrough_without_llm():
    inner = _StubInner({})
    wrap = ErrorAwareRetryProvider(inner, llm=None)
    r = await wrap.invoke(_call())
    assert r.ok is False


async def test_passthrough_on_transient_error():
    """B-17 already handles transient — don't double-fixup."""
    inner = _StubInner({})  # → returns "no stub" error which contains nothing transient
    # Override the result manually for this test:
    inner.results = {
        ("file_read", '{"path": "/missing.txt"}'):
            ToolResult(call_id="t", ok=False, content=None,
                       error="Connection refused"),
    }
    llm = _StubLLM(response='{"action":"retry","new_args":{"path":"/x"}}')
    wrap = ErrorAwareRetryProvider(inner, llm=llm)
    r = await wrap.invoke(_call())
    assert r.ok is False
    assert llm.calls == 0  # transient → skip fixup


# ── Retry / alternative / skip ───────────────────────────────────


async def test_fixup_retry_with_new_args():
    """LLM says retry with different path; that path succeeds."""
    inner = _StubInner({
        ("file_read", '{"path": "/missing.txt"}'):
            _fail("FileNotFoundError: /missing.txt"),
        ("file_read", '{"path": "/real.txt"}'):
            _ok("file content"),
    })
    llm = _StubLLM(response=json.dumps({
        "action": "retry",
        "new_args": {"path": "/real.txt"},
        "reason": "user probably meant the real path",
    }))
    wrap = ErrorAwareRetryProvider(inner, llm=llm)
    r = await wrap.invoke(_call())
    assert r.ok is True
    assert r.content == "file content"
    assert wrap._fixups_attempted == 1
    assert wrap._fixups_succeeded == 1


async def test_fixup_retry_still_fails_returns_original():
    """Retry attempted but second call also fails — return original error."""
    inner = _StubInner({
        ("file_read", '{"path": "/missing.txt"}'):
            _fail("FileNotFoundError: /missing.txt"),
        ("file_read", '{"path": "/also_missing.txt"}'):
            _fail("FileNotFoundError: /also_missing.txt"),
    })
    llm = _StubLLM(response=json.dumps({
        "action": "retry",
        "new_args": {"path": "/also_missing.txt"},
    }))
    wrap = ErrorAwareRetryProvider(inner, llm=llm)
    r = await wrap.invoke(_call())
    assert r.ok is False
    assert "/missing.txt" in r.error  # original error bubbled up
    assert wrap._fixups_attempted == 1
    assert wrap._fixups_succeeded == 0


async def test_fixup_alternative_tool():
    """LLM picks an alternative tool that exists in catalog."""
    inner = _StubInner({
        ("file_read", '{"path": "/x"}'): _fail("path not safe"),
        ("file_read_safe", '{"path": "/x"}'): _ok("safe content"),
    })
    llm = _StubLLM(response=json.dumps({
        "action": "alternative",
        "new_tool": "file_read_safe",
        "new_args": {"path": "/x"},
    }))
    wrap = ErrorAwareRetryProvider(inner, llm=llm)
    r = await wrap.invoke(_call("file_read", {"path": "/x"}))
    assert r.ok is True
    assert r.content == "safe content"


async def test_fixup_alternative_unknown_tool_rejected():
    """LLM hallucinates an alternative — wrapper must REFUSE (not call
    nonexistent tool) and bubble original error."""
    inner = _StubInner({
        ("file_read", '{"path": "/x"}'): _fail("nope"),
    })
    llm = _StubLLM(response=json.dumps({
        "action": "alternative",
        "new_tool": "make_up_a_tool",
        "new_args": {},
    }))
    wrap = ErrorAwareRetryProvider(inner, llm=llm)
    r = await wrap.invoke(_call("file_read", {"path": "/x"}))
    assert r.ok is False
    assert "nope" in r.error


async def test_fixup_skip():
    """LLM says skip — original error returned, no second invoke."""
    inner = _StubInner({("file_read", '{"path": "/x"}'): _fail("perm denied")})
    llm = _StubLLM(response='{"action":"skip","reason":"can\'t help"}')
    wrap = ErrorAwareRetryProvider(inner, llm=llm)
    r = await wrap.invoke(_call("file_read", {"path": "/x"}))
    assert r.ok is False
    assert "perm denied" in r.error
    # inner invoked exactly once (no second attempt)
    assert len(inner.invoke_log) == 1


async def test_fixup_bad_json_bubbles_original():
    inner = _StubInner({("file_read", '{"path": "/x"}'): _fail("err")})
    llm = _StubLLM(response="not json at all, just prose")
    wrap = ErrorAwareRetryProvider(inner, llm=llm)
    r = await wrap.invoke(_call("file_read", {"path": "/x"}))
    assert r.ok is False
    assert r.error == "err"


async def test_fixup_llm_timeout_bubbles_original():
    inner = _StubInner({("file_read", '{"path": "/x"}'): _fail("err")})

    class _SlowLLM:
        async def complete(self, *a, **kw):
            await asyncio.sleep(2.0)

            class _R:
                content = ""
            return _R()

    wrap = ErrorAwareRetryProvider(inner, llm=_SlowLLM(), timeout_s=0.3)
    r = await wrap.invoke(_call("file_read", {"path": "/x"}))
    assert r.ok is False
    assert r.error == "err"


async def test_fixup_llm_raises_bubbles_original():
    inner = _StubInner({("file_read", '{"path": "/x"}'): _fail("err")})
    wrap = ErrorAwareRetryProvider(
        inner, llm=_StubLLM(raises=RuntimeError("boom")),
    )
    r = await wrap.invoke(_call("file_read", {"path": "/x"}))
    assert r.ok is False
    assert r.error == "err"


# ── Late LLM binding (factory pattern) ───────────────────────────


async def test_set_llm_late_binding():
    """build_tools_from_config constructs the wrapper without an LLM;
    build_agent_from_config calls set_llm later. Test both phases."""
    inner = _StubInner({
        ("file_read", '{"path": "/missing"}'): _fail("FileNotFoundError"),
        ("file_read", '{"path": "/fixed"}'): _ok("content"),
    })
    wrap = ErrorAwareRetryProvider(inner, llm=None)
    # Phase 1: no LLM → passthrough
    r1 = await wrap.invoke(_call("file_read", {"path": "/missing"}))
    assert r1.ok is False
    # Phase 2: LLM plumbed in → fixup attempts
    wrap.set_llm(_StubLLM(response=json.dumps({
        "action": "retry", "new_args": {"path": "/fixed"},
    })))
    r2 = await wrap.invoke(_call("file_read", {"path": "/missing"}))
    assert r2.ok is True


# ── Parser unit tests ────────────────────────────────────────────


def test_parse_fixup_strict_json():
    out = _parse_fixup_json('{"action": "skip"}')
    assert out == {"action": "skip"}


def test_parse_fixup_fenced():
    out = _parse_fixup_json('```json\n{"action":"retry","new_args":{}}\n```')
    assert out is not None and out["action"] == "retry"


def test_parse_fixup_rejects_no_action_field():
    """Object without an ``action`` key is not a valid fixup."""
    assert _parse_fixup_json('{"foo": "bar"}') is None


def test_parse_fixup_empty():
    assert _parse_fixup_json("") is None
    assert _parse_fixup_json("just prose") is None
