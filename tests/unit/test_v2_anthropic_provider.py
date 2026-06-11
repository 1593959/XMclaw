"""AnthropicLLM — pure-transform and lazy-client unit tests.

These tests don't hit the network. The SDK client is monkey-patched for
the one test that exercises ``complete`` end-to-end on a faked response.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from xmclaw.core.ir import ToolCall, ToolCallShape, ToolSpec
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.llm.base import Message


# ── pure transforms ────────────────────────────────────────────────────────

def test_messages_to_anthropic_splits_system() -> None:
    """B-245 update: system is now emitted as a list of content blocks
    with cache_control on the single text block (was: plain string).
    Anthropic SDK accepts both shapes; we always send blocks now to
    enable prompt caching."""
    system, msgs = AnthropicLLM._messages_to_anthropic([
        Message(role="system", content="you are a helper"),
        Message(role="user", content="hi"),
    ])
    assert isinstance(system, list)
    assert system[0]["text"] == "you are a helper"
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"


def test_messages_to_anthropic_emits_tool_use_blocks() -> None:
    tc = ToolCall(name="foo", args={"k": 1}, provenance="synthetic", id="tc-1")
    msgs = [
        Message(role="assistant", content="let me check", tool_calls=(tc,)),
    ]
    _, converted = AnthropicLLM._messages_to_anthropic(msgs)
    blocks = converted[0]["content"]
    assert isinstance(blocks, list)
    assert any(
        b["type"] == "tool_use" and b["name"] == "foo" and b["id"] == "tc-1"
        for b in blocks
    )


def test_messages_to_anthropic_emits_tool_result() -> None:
    # 2026-06-10: a tool_result is only valid right after its tool_use
    # (the repair pass downgrades orphans) — so pair it properly here.
    tc = ToolCall(name="foo", args={}, provenance="synthetic", id="tc-1")
    msgs = [
        Message(role="assistant", content="", tool_calls=(tc,)),
        Message(role="tool", content="42", tool_call_id="tc-1"),
    ]
    _, converted = AnthropicLLM._messages_to_anthropic(msgs)
    assert converted[1]["role"] == "user"
    assert converted[1]["content"][0]["type"] == "tool_result"
    assert converted[1]["content"][0]["tool_use_id"] == "tc-1"


# ── 2026-06-10: tool_use/tool_result pairing repair ──────────────────
# Strict anthropic-compat endpoints (DeepSeek) 400 the WHOLE request on
# any pairing violation. Violations enter history via model switches
# (OpenAI-provider turns replayed here), crashed turns, or pruning.


def test_repair_synthesizes_missing_tool_result() -> None:
    """REGRESSION (图二): assistant tool_use whose result vanished from
    history must get a placeholder tool_result in the NEXT message —
    otherwise DeepSeek 400s: ``tool_use ids were found without
    tool_result blocks immediately after``."""
    tc = ToolCall(name="bash", args={"command": "ls"},
                  provenance="synthetic", id="call_01_lost")
    msgs = [
        Message(role="user", content="run ls"),
        Message(role="assistant", content="", tool_calls=(tc,)),
        # ← tool result missing (interrupted turn / model switch)
        Message(role="user", content="and then?"),
    ]
    _, converted = AnthropicLLM._messages_to_anthropic(msgs)
    # The message right after the tool_use must carry the matching result.
    use_idx = next(
        i for i, m in enumerate(converted)
        if isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_use" for b in m["content"])
    )
    nxt = converted[use_idx + 1]
    assert nxt["role"] == "user"
    results = [b for b in nxt["content"] if b.get("type") == "tool_result"]
    assert results and results[0]["tool_use_id"] == "call_01_lost"


def test_repair_merges_multi_tool_results_into_one_message() -> None:
    """Two tool calls in one assistant turn produce two role=tool
    messages; Anthropic requires BOTH results in the single next
    message. The repair pass must merge them."""
    t1 = ToolCall(name="a", args={}, provenance="synthetic", id="id-1")
    t2 = ToolCall(name="b", args={}, provenance="synthetic", id="id-2")
    msgs = [
        Message(role="assistant", content="", tool_calls=(t1, t2)),
        Message(role="tool", content="r1", tool_call_id="id-1"),
        Message(role="tool", content="r2", tool_call_id="id-2"),
    ]
    _, converted = AnthropicLLM._messages_to_anthropic(msgs)
    assert len(converted) == 2, f"results not merged: {converted}"
    ids = {b["tool_use_id"] for b in converted[1]["content"]
           if b.get("type") == "tool_result"}
    assert ids == {"id-1", "id-2"}


def test_repair_downgrades_orphan_tool_result_to_text() -> None:
    """A tool_result with no matching tool_use right before it (the
    other half of the cross-provider corruption) must not be sent as a
    tool_result — strict endpoints 400 on unexpected tool_use_id."""
    msgs = [
        Message(role="user", content="hi"),
        Message(role="tool", content="stray", tool_call_id="ghost-1"),
    ]
    _, converted = AnthropicLLM._messages_to_anthropic(msgs)
    import json as _json
    assert "tool_result" not in _json.dumps(converted)
    assert "stray" in _json.dumps(converted)  # content preserved as text


def test_repair_trailing_tool_use_gets_placeholder() -> None:
    """History ending ON a tool_use (turn died mid-dispatch) must close
    the pair so the next request is valid."""
    tc = ToolCall(name="x", args={}, provenance="synthetic", id="tail-1")
    msgs = [Message(role="assistant", content="", tool_calls=(tc,))]
    _, converted = AnthropicLLM._messages_to_anthropic(msgs)
    assert converted[-1]["role"] == "user"
    assert converted[-1]["content"][0]["type"] == "tool_result"
    assert converted[-1]["content"][0]["tool_use_id"] == "tail-1"


def test_tools_to_anthropic_format() -> None:
    specs = [
        ToolSpec(name="read", description="read a file",
                 parameters_schema={"type": "object", "properties": {"p": {"type": "string"}}}),
    ]
    out = AnthropicLLM._tools_to_anthropic(specs)
    assert out[0]["name"] == "read"
    assert "input_schema" in out[0]
    assert out[0]["input_schema"]["type"] == "object"


def test_empty_tools_emits_empty_list() -> None:
    assert AnthropicLLM._tools_to_anthropic(None) == []
    assert AnthropicLLM._tools_to_anthropic([]) == []


def test_system_cache_breakpoint_marker_splits_into_blocks() -> None:
    """Wave-30 prompt-cache fix: the CACHE_BREAKPOINT_MARKER sentinel
    inside ``Message(role="system").content`` splits into independent
    text blocks. Every block EXCEPT the last gets cache_control —
    the trailing mutable tail (time block) stays out of the cached
    prefix so it doesn't invalidate the cache every turn."""
    from xmclaw.providers.llm.base import CACHE_BREAKPOINT_MARKER

    sys_text = (
        "you are a helper"
        + f"\n\n{CACHE_BREAKPOINT_MARKER}\n\n"
        + "## What I remember about you\n* name: He"
        + f"\n\n{CACHE_BREAKPOINT_MARKER}\n\n"
        + "## 当前时刻\n\n2026-05-18 02:30:00"
    )
    system, _ = AnthropicLLM._messages_to_anthropic([
        Message(role="system", content=sys_text),
        Message(role="user", content="hi"),
    ])
    # Three blocks because we put two markers in.
    assert isinstance(system, list)
    assert len(system) == 3
    # First two are cacheable, last (mutable time tail) is not.
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[2]
    # Content order preserved + marker stripped.
    assert "you are a helper" in system[0]["text"]
    assert "What I remember" in system[1]["text"]
    assert "当前时刻" in system[2]["text"]
    for block in system:
        assert CACHE_BREAKPOINT_MARKER not in block["text"]


def test_tools_cache_breakpoint_skips_prefilter_skills() -> None:
    """Wave-30 prompt-cache fix: the tools-array cache_control marker
    moves to the LAST STABLE tool (the one just before the first
    ``skill_*``) so the per-turn prefilter output doesn't invalidate
    the cache every turn. Stable tools (bash, file_read, etc.) live
    at indices [0..N); skills at [N..end)."""
    specs = [
        ToolSpec(name="file_read", description="read",
                 parameters_schema={"type": "object"}),
        ToolSpec(name="bash", description="run",
                 parameters_schema={"type": "object"}),
        ToolSpec(name="skill_git-commit", description="commit",
                 parameters_schema={"type": "object"}),
        ToolSpec(name="skill_review", description="review",
                 parameters_schema={"type": "object"}),
    ]
    out = AnthropicLLM._tools_to_anthropic(specs)
    # Breakpoint should be on ``bash`` (last stable), NOT on the
    # final ``skill_review`` (which is per-turn volatile).
    assert out[1]["name"] == "bash"
    assert out[1].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in out[3]  # skill_review — no marker


def test_tools_cache_breakpoint_falls_back_to_last_when_no_skills() -> None:
    """When the tool list has no ``skill_*`` entries (small setup
    below the B-238 prefilter threshold) the breakpoint stays on
    the last tool — preserves the pre-Wave-30 behaviour."""
    specs = [
        ToolSpec(name="file_read", description="read",
                 parameters_schema={"type": "object"}),
        ToolSpec(name="bash", description="run",
                 parameters_schema={"type": "object"}),
    ]
    out = AnthropicLLM._tools_to_anthropic(specs)
    assert out[-1].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in out[0]


def test_history_cache_breakpoint_marks_last_message() -> None:
    """Wave-30 follow-up (2026-05-18): the 4th cache breakpoint goes
    on the LAST message so prior history is cached too. Pre-fix a
    multi-turn chat re-billed ~28K tokens of prior history on every
    LLM call because Anthropic does NOT auto-cache messages — only
    positions explicitly marked with cache_control. Verified
    empirically against Kimi K2.6 (Anthropic-compat): a 2611-token
    conversation now bills 0 fresh input + 2611 cache_read on the
    second call within the cache window."""
    msgs = [
        Message(role="system", content="be terse"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
        Message(role="user", content="continue"),
    ]
    _, converted = AnthropicLLM._messages_to_anthropic(msgs)
    assert len(converted) == 3
    # First two messages keep plain-string content (anti-req #11
    # non-interference — match what a naked SDK caller would send).
    assert converted[0]["content"] == "hi"
    assert converted[1]["content"] == "hello"
    # Last message's content is now a block list with cache_control.
    last = converted[-1]
    assert isinstance(last["content"], list)
    assert last["content"][0]["text"] == "continue"
    assert last["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_history_cache_breakpoint_tags_existing_block_content() -> None:
    """When the last message already has block-shape content (tool_use
    or image attachment), tag the trailing block in place rather than
    wrapping anew."""
    tc = ToolCall(name="read", args={}, provenance="synthetic", id="t1")
    msgs = [
        Message(role="user", content="hi"),
        Message(role="assistant", content="checking", tool_calls=(tc,)),
    ]
    _, converted = AnthropicLLM._messages_to_anthropic(msgs)
    last = converted[-1]
    assert isinstance(last["content"], list)
    # 2026-06-10: history ending on a tool_use gets a synthesized
    # placeholder tool_result appended (pairing repair), so the LAST
    # message is now that result — and IT carries the cache marker.
    assert last["content"][-1]["type"] == "tool_result"
    assert last["content"][-1].get("cache_control") == {"type": "ephemeral"}
    # The tool_use itself is still present, untagged, one message back.
    prev = converted[-2]
    assert any(b.get("type") == "tool_use" for b in prev["content"])


def test_history_cache_breakpoint_skips_empty_messages() -> None:
    """If the message list is empty, no marker work. (Pre-fix would
    have IndexError'd on converted[-1].)"""
    _, converted = AnthropicLLM._messages_to_anthropic([
        Message(role="system", content="x"),
    ])
    assert converted == []


# ── properties ────────────────────────────────────────────────────────────

def test_tool_call_shape_is_anthropic_native() -> None:
    llm = AnthropicLLM(api_key="x")
    assert llm.tool_call_shape == ToolCallShape.ANTHROPIC_NATIVE


def test_default_pricing_non_zero() -> None:
    llm = AnthropicLLM(api_key="x")
    assert llm.pricing.input_per_mtok > 0
    assert llm.pricing.output_per_mtok > 0


# ── complete() with a faked client ────────────────────────────────────────

@dataclass
class _FakeBlock:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict | None = None


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeResponse:
    content: list
    usage: _FakeUsage


class _FakeMessagesAPI:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def create(self, **kwargs):  # noqa: ANN003, ARG002
        return self._response


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessagesAPI(response)


@pytest.mark.asyncio
async def test_complete_parses_text_and_tool_use_blocks() -> None:
    fake_response = _FakeResponse(
        content=[
            _FakeBlock(type="text", text="Here is the summary."),
            _FakeBlock(type="tool_use", id="toolu_1", name="file_read",
                       input={"path": "/tmp/x"}),
        ],
        usage=_FakeUsage(input_tokens=42, output_tokens=17),
    )
    llm = AnthropicLLM(api_key="x")
    llm._client = _FakeClient(fake_response)

    resp = await llm.complete([Message(role="user", content="summarize X")])
    assert resp.content == "Here is the summary."
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "file_read"
    assert resp.tool_calls[0].args == {"path": "/tmp/x"}
    assert resp.prompt_tokens == 42
    assert resp.completion_tokens == 17
    assert resp.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_complete_with_no_tool_calls() -> None:
    fake_response = _FakeResponse(
        content=[_FakeBlock(type="text", text="plain text")],
        usage=_FakeUsage(input_tokens=1, output_tokens=1),
    )
    llm = AnthropicLLM(api_key="x")
    llm._client = _FakeClient(fake_response)
    resp = await llm.complete([Message(role="user", content="x")])
    assert resp.content == "plain text"
    assert resp.tool_calls == ()


@pytest.mark.asyncio
async def test_complete_rejects_malformed_tool_use_block() -> None:
    """Anti-req #1: a malformed tool_use block does NOT produce a ToolCall."""
    fake_response = _FakeResponse(
        content=[
            # Missing 'input' as dict — translator returns None, we drop it.
            _FakeBlock(type="tool_use", id="t", name="foo", input="not-a-dict"),
        ],
        usage=_FakeUsage(input_tokens=1, output_tokens=1),
    )
    llm = AnthropicLLM(api_key="x")
    llm._client = _FakeClient(fake_response)
    resp = await llm.complete([Message(role="user", content="x")])
    # No tool calls — malformed block silently dropped (translator returned None).
    assert resp.tool_calls == ()


# ── Epic #27 sweep #14 (2026-05-19): max_tokens configurable ──────


class _KwargsCapturingMessagesAPI:
    """Like _FakeMessagesAPI but captures kwargs so tests can assert
    what was passed to the Anthropic SDK."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_kwargs: dict = {}

    async def create(self, **kwargs):  # noqa: ANN003
        self.last_kwargs = kwargs
        return self._response


class _CapturingClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _KwargsCapturingMessagesAPI(response)


@pytest.mark.asyncio
async def test_max_tokens_default_is_8192() -> None:
    """Pre-fix three call sites hard-coded 4096 — long outputs
    truncated silently with B-229's "partial tool call dropped"
    marker. Default bumped to Anthropic's documented default for
    opus/sonnet so vision-heavy + long-reasoning workflows stop
    hitting the cap by accident."""
    llm = AnthropicLLM(api_key="x")
    assert llm.max_tokens == 8192


@pytest.mark.asyncio
async def test_max_tokens_constructor_override() -> None:
    """Caller can dial it up (or down) via constructor — flows
    in from ``factory.py`` reading ``llm.anthropic.max_tokens``."""
    llm = AnthropicLLM(api_key="x", max_tokens=32000)
    assert llm.max_tokens == 32000


@pytest.mark.asyncio
async def test_complete_sends_configured_max_tokens_to_sdk() -> None:
    """End-to-end: the max_tokens kwarg lands on the Anthropic
    SDK call. Pre-fix this asserted 4096 regardless of override."""
    fake_response = _FakeResponse(
        content=[_FakeBlock(type="text", text="hi")],
        usage=_FakeUsage(input_tokens=1, output_tokens=1),
    )
    llm = AnthropicLLM(api_key="x", max_tokens=16384)
    client = _CapturingClient(fake_response)
    llm._client = client
    await llm.complete([Message(role="user", content="x")])
    assert client.messages.last_kwargs["max_tokens"] == 16384
