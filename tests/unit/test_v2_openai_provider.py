"""OpenAILLM — pure-transform and faked-client unit tests.

Same shape as test_v2_anthropic_provider.py — tests live offline; the
one ``complete()`` test exercises the full extraction path against a
fake ``AsyncOpenAI`` response.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from xmclaw.core.ir import ToolCall, ToolCallShape, ToolSpec
from xmclaw.providers.llm.base import Message
from xmclaw.providers.llm.openai import OpenAILLM


# ── pure transforms ───────────────────────────────────────────────────────

def test_messages_to_openai_keeps_system_inline() -> None:
    """OpenAI convention: system prompt stays in the messages array
    (unlike Anthropic which moves it out)."""
    msgs = OpenAILLM._messages_to_openai([
        Message(role="system", content="you are a helper"),
        Message(role="user", content="hi"),
    ])
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "you are a helper"


# A tiny valid data: URL (pass-through path of _img_to_data_url — no
# Pillow / disk I/O needed for the serialization-shape assertions).
_DATA_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"


def test_image_dropped_for_non_vision_model() -> None:
    """REGRESSION (2026-06-08): switching a chat with image history from a
    vision model (Kimi) to a text-only one (DeepSeek-V4-Pro) 400'd the
    whole turn — ``unknown variant `image_url`, expected `text```. A
    text-only model must NOT receive image_url blocks; the image degrades
    to a text placeholder so history stays valid."""
    msgs = [Message(role="user", content="看这张图", images=(_DATA_URL,))]
    out = OpenAILLM._messages_to_openai(
        msgs, model="DeepSeek-V4-Pro", base_url="https://api.deepseek.com/v1",
    )
    assert len(out) == 1
    blob = json.dumps(out, ensure_ascii=False)
    assert "image_url" not in blob, f"image_url leaked to text-only model: {out}"
    # content stays a plain string (not a multimodal block list) and is
    # non-empty (placeholder appended).
    assert isinstance(out[0]["content"], str)
    assert "看这张图" in out[0]["content"]
    assert "图片" in out[0]["content"]  # placeholder present


def test_image_kept_for_vision_model() -> None:
    """A vision-capable model (gpt-4o) still gets a proper image_url
    multimodal block — the gate must not over-strip."""
    msgs = [Message(role="user", content="看这张图", images=(_DATA_URL,))]
    out = OpenAILLM._messages_to_openai(
        msgs, model="gpt-4o", base_url="https://api.openai.com/v1",
    )
    assert len(out) == 1
    assert isinstance(out[0]["content"], list)  # multimodal block list
    kinds = {b.get("type") for b in out[0]["content"]}
    assert "image_url" in kinds, f"vision model lost its image block: {out}"


def test_image_only_message_not_empty_for_non_vision() -> None:
    """A user message that was ONLY an image (no text) must still produce
    non-empty content for a text-only model — some endpoints 400 on empty
    user content."""
    msgs = [Message(role="user", content="", images=(_DATA_URL,))]
    out = OpenAILLM._messages_to_openai(
        msgs, model="DeepSeek-V4-Pro", base_url="https://api.deepseek.com/v1",
    )
    assert out[0]["content"].strip(), "image-only message degraded to empty content"
    assert "image_url" not in json.dumps(out)


def test_messages_to_openai_emits_tool_calls_on_assistant() -> None:
    tc = ToolCall(name="foo", args={"k": 1}, provenance="synthetic", id="call-1")
    msgs = [Message(role="assistant", content="", tool_calls=(tc,))]
    out = OpenAILLM._messages_to_openai(msgs)
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert "tool_calls" in out[0]
    assert out[0]["tool_calls"][0]["function"]["name"] == "foo"
    # arguments round-trips as JSON string
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {"k": 1}


def test_messages_to_openai_emits_tool_result_with_tool_call_id() -> None:
    msgs = [Message(role="tool", content="42", tool_call_id="call-1")]
    out = OpenAILLM._messages_to_openai(msgs)
    assert out[0]["role"] == "tool"
    assert out[0]["tool_call_id"] == "call-1"
    assert out[0]["content"] == "42"


def test_tools_to_openai_format() -> None:
    specs = [
        ToolSpec(name="read", description="read a file",
                 parameters_schema={"type": "object", "properties": {"p": {"type": "string"}}}),
    ]
    out = OpenAILLM._tools_to_openai(specs)
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "read"
    assert "parameters" in out[0]["function"]


def test_empty_tools() -> None:
    assert OpenAILLM._tools_to_openai(None) == []
    assert OpenAILLM._tools_to_openai([]) == []


# ── properties ────────────────────────────────────────────────────────────

def test_tool_call_shape_is_openai_tool() -> None:
    assert OpenAILLM(api_key="x").tool_call_shape == ToolCallShape.OPENAI_TOOL


def test_default_pricing_is_non_zero_for_openai_proper() -> None:
    p = OpenAILLM(api_key="x").pricing
    assert p.input_per_mtok > 0
    assert p.output_per_mtok > 0


def test_base_url_stored_for_compat_endpoints() -> None:
    llm = OpenAILLM(api_key="x", base_url="https://compat.example/v1")
    assert llm.base_url == "https://compat.example/v1"


# ── complete() against a faked client ─────────────────────────────────────

@dataclass
class _FakeFunction:
    name: str
    arguments: str


@dataclass
class _FakeToolCall:
    id: str
    type: str
    function: _FakeFunction


@dataclass
class _FakeMessage:
    content: str
    tool_calls: list | None = None


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _FakeResponse:
    choices: list
    usage: _FakeUsage


class _FakeChatCompletionsAPI:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def create(self, **kwargs: Any) -> _FakeResponse:  # noqa: ARG002
        return self._response


class _FakeChatAPI:
    def __init__(self, response: _FakeResponse) -> None:
        self.completions = _FakeChatCompletionsAPI(response)


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.chat = _FakeChatAPI(response)


@pytest.mark.asyncio
async def test_complete_parses_text_and_tool_calls() -> None:
    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content="Here is the summary.",
                tool_calls=[_FakeToolCall(
                    id="call_1",
                    type="function",
                    function=_FakeFunction(
                        name="file_read",
                        arguments=json.dumps({"path": "/tmp/x"}),
                    ),
                )],
            ),
        )],
        usage=_FakeUsage(prompt_tokens=42, completion_tokens=17),
    )
    llm = OpenAILLM(api_key="x")
    llm._client = _FakeClient(fake)

    resp = await llm.complete([Message(role="user", content="summarize")])
    assert resp.content == "Here is the summary."
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "file_read"
    assert resp.tool_calls[0].args == {"path": "/tmp/x"}
    assert resp.prompt_tokens == 42
    assert resp.completion_tokens == 17
    assert resp.latency_ms >= 0.0


@pytest.mark.asyncio
async def test_complete_with_no_tool_calls() -> None:
    fake = _FakeResponse(
        choices=[_FakeChoice(message=_FakeMessage(content="just text"))],
        usage=_FakeUsage(prompt_tokens=1, completion_tokens=1),
    )
    llm = OpenAILLM(api_key="x")
    llm._client = _FakeClient(fake)
    resp = await llm.complete([Message(role="user", content="x")])
    assert resp.content == "just text"
    assert resp.tool_calls == ()


@pytest.mark.asyncio
async def test_complete_drops_malformed_tool_call() -> None:
    """Anti-req #1: malformed arguments → translator returns None → dropped."""
    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content="",
                tool_calls=[_FakeToolCall(
                    id="call_x",
                    type="function",
                    function=_FakeFunction(name="f", arguments="{not json"),
                )],
            ),
        )],
        usage=_FakeUsage(prompt_tokens=1, completion_tokens=1),
    )
    llm = OpenAILLM(api_key="x")
    llm._client = _FakeClient(fake)
    resp = await llm.complete([Message(role="user", content="x")])
    assert resp.tool_calls == ()


@pytest.mark.asyncio
async def test_complete_handles_empty_choices() -> None:
    fake = _FakeResponse(choices=[], usage=_FakeUsage(prompt_tokens=0, completion_tokens=0))
    llm = OpenAILLM(api_key="x")
    llm._client = _FakeClient(fake)
    resp = await llm.complete([Message(role="user", content="x")])
    assert resp.content == ""
    assert resp.tool_calls == ()


# ── B-320 prompt cache parity ────────────────────────────────────────────


def test_b320_messages_no_cache_marker_when_disabled() -> None:
    """Default OFF: system message stays as a plain string — never
    decorated with cache_control. This is the conservative behavior
    for OpenAI proper / DeepSeek / strict-schema compat servers that
    reject unknown body fields."""
    msgs = OpenAILLM._messages_to_openai(
        [
            Message(role="system", content="you are a helper"),
            Message(role="user", content="hi"),
        ],
        prompt_cache_enabled=False,
    )
    assert msgs[0]["content"] == "you are a helper"
    assert "cache_control" not in str(msgs)


def test_b320_messages_decorate_last_system_when_enabled() -> None:
    """B-320: prompt_cache_enabled=True wraps the LAST system message
    in an Anthropic-style content-block list with
    cache_control=ephemeral. The marker covers everything before it
    (system + tools, hash-stable across hops in a turn) → ~10% list
    price for cache reads on Moonshot Kimi / Zhipu GLM."""
    msgs = OpenAILLM._messages_to_openai(
        [
            Message(role="system", content="be terse"),
            Message(role="system", content="answer in english"),
            Message(role="user", content="hi"),
        ],
        prompt_cache_enabled=True,
    )
    # First system message stays plain.
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "be terse"
    # Second (last) system message is decorated.
    assert msgs[1]["role"] == "system"
    blocks = msgs[1]["content"]
    assert isinstance(blocks, list) and len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == "answer in english"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_wave30_system_cache_breakpoint_marker_splits_into_blocks() -> None:
    """Wave-30 prompt-cache fix: CACHE_BREAKPOINT_MARKER inside the
    last system message splits into independent text blocks; every
    block EXCEPT the last gets cache_control. Per-turn mutable tail
    (time block) stays out of the cached prefix."""
    from xmclaw.providers.llm.base import CACHE_BREAKPOINT_MARKER

    sys_text = (
        "stable prefix"
        + f"\n\n{CACHE_BREAKPOINT_MARKER}\n\n"
        + "mutable time block"
    )
    msgs = OpenAILLM._messages_to_openai(
        [
            Message(role="system", content=sys_text),
            Message(role="user", content="hi"),
        ],
        prompt_cache_enabled=True,
    )
    sys_msg = msgs[0]
    blocks = sys_msg["content"]
    assert isinstance(blocks, list) and len(blocks) == 2
    # First (stable) is cacheable.
    assert blocks[0]["text"] == "stable prefix"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    # Last (mutable tail) is NOT cacheable.
    assert blocks[1]["text"] == "mutable time block"
    assert "cache_control" not in blocks[1]
    # Marker stripped from rendered text.
    for b in blocks:
        assert CACHE_BREAKPOINT_MARKER not in b["text"]


def test_wave30_system_cache_marker_stripped_when_cache_disabled() -> None:
    """When prompt_cache_enabled=False (standard OpenAI / DeepSeek /
    unknown shims), the sentinel must NOT leak into the system text
    the model sees — replace it with plain double-newlines."""
    from xmclaw.providers.llm.base import CACHE_BREAKPOINT_MARKER

    sys_text = f"prefix\n\n{CACHE_BREAKPOINT_MARKER}\n\ntail"
    msgs = OpenAILLM._messages_to_openai(
        [
            Message(role="system", content=sys_text),
            Message(role="user", content="hi"),
        ],
        prompt_cache_enabled=False,
    )
    sys_msg = msgs[0]
    # No content-block list when cache is disabled — plain string.
    assert isinstance(sys_msg["content"], str)
    assert CACHE_BREAKPOINT_MARKER not in sys_msg["content"]
    assert "prefix" in sys_msg["content"]
    assert "tail" in sys_msg["content"]


def test_wave30_tools_cache_breakpoint_skips_prefilter_skills() -> None:
    """Wave-30 prompt-cache fix: tools-array breakpoint moves to the
    LAST STABLE tool (just before the first ``skill_*``), so the
    per-turn prefilter output doesn't invalidate the cache every
    turn. Mirror of the anthropic-side fix."""
    specs = [
        ToolSpec(name="file_read", description="r",
                 parameters_schema={"type": "object", "properties": {}}),
        ToolSpec(name="bash", description="run",
                 parameters_schema={"type": "object", "properties": {}}),
        ToolSpec(name="skill_git", description="commit",
                 parameters_schema={"type": "object", "properties": {}}),
        ToolSpec(name="skill_review", description="review",
                 parameters_schema={"type": "object", "properties": {}}),
    ]
    out = OpenAILLM._tools_to_openai(specs, prompt_cache_enabled=True)
    assert out[1]["function"]["name"] == "bash"
    assert out[1].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in out[2]
    assert "cache_control" not in out[3]


def test_b320_tools_decorate_last_when_enabled() -> None:
    """B-320: cache_control on the last tool entry is the breakpoint
    that covers every preceding tool def in one cache slot. Mirror
    of AnthropicLLM._tools_to_anthropic."""
    specs = [
        ToolSpec(
            name="a", description="first",
            parameters_schema={"type": "object", "properties": {}},
        ),
        ToolSpec(
            name="b", description="second",
            parameters_schema={"type": "object", "properties": {}},
        ),
    ]
    out = OpenAILLM._tools_to_openai(specs, prompt_cache_enabled=True)
    assert "cache_control" not in out[0]
    assert out[1]["cache_control"] == {"type": "ephemeral"}


def test_b320_tools_no_marker_when_disabled() -> None:
    specs = [
        ToolSpec(
            name="a", description="d",
            parameters_schema={"type": "object", "properties": {}},
        ),
    ]
    out = OpenAILLM._tools_to_openai(specs, prompt_cache_enabled=False)
    assert all("cache_control" not in t for t in out)


@pytest.mark.parametrize("b_url, model, expected", [
    # OpenAI proper → off (auto cache, no opt-in needed; unknown
    # fields would cause 400 on stricter shims downstream).
    (None, "gpt-4o", False),
    # DeepSeek → off (auto caching, OpenAI-style usage report).
    ("https://api.deepseek.com/v1", "deepseek-chat", False),
    # Moonshot Kimi → on (their compat shim accepts the marker).
    ("https://api.moonshot.cn/v1", "moonshot-v1-128k", True),
    ("https://api.openai.com/v1", "kimi-k2-0905-preview", True),
    # Zhipu GLM → on.
    ("https://open.bigmodel.cn/api/paas/v4", "glm-4.6", True),
    ("https://api.z.ai/api/paas/v4", "glm-4.6", True),
    # Generic compat (vLLM / LiteLLM / Ollama) → off, conservative.
    ("http://localhost:11434/v1", "qwen2.5:7b", False),
])
def test_b320_default_prompt_cache_enabled_per_provider(
    b_url, model, expected,
) -> None:
    """B-320: auto-detect picks safe defaults — known cache-supporting
    OpenAI-compat shims are on, everything else is off.

    NOTE: arg name is ``b_url`` not ``base_url`` because pytest's
    ``pytest_base_url`` plugin owns the latter as a session-scoped
    fixture and parametrize would collide.
    """
    from xmclaw.providers.llm.openai import _default_prompt_cache_enabled
    assert _default_prompt_cache_enabled(model, b_url) is expected


def test_b320_constructor_override_wins() -> None:
    """Explicit ``prompt_cache_enabled=False`` on a Moonshot URL
    disables caching — the user's override wins over auto-detect."""
    llm = OpenAILLM(
        api_key="x",
        model="moonshot-v1-128k",
        base_url="https://api.moonshot.cn/v1",
        prompt_cache_enabled=False,
    )
    assert llm._prompt_cache_enabled is False


def test_b320_constructor_auto_detect_when_none() -> None:
    """Default: passing None lets auto-detect pick. Moonshot → True."""
    llm = OpenAILLM(
        api_key="x",
        model="moonshot-v1-128k",
        base_url="https://api.moonshot.cn/v1",
    )
    assert llm._prompt_cache_enabled is True


def test_b320_extract_cache_tokens_handles_anthropic_style_flat() -> None:
    """Moonshot / Zhipu mirror Anthropic's flat field names on usage."""
    @dataclass
    class _U:
        cache_creation_input_tokens: int = 0
        cache_read_input_tokens: int = 0

    cc, cr = OpenAILLM._extract_cache_tokens(_U(123, 456))
    assert cc == 123
    assert cr == 456


def test_b320_extract_cache_tokens_handles_openai_nested() -> None:
    """OpenAI / DeepSeek auto cache: ``prompt_tokens_details.cached_tokens``."""
    @dataclass
    class _Details:
        cached_tokens: int = 999

    @dataclass
    class _U:
        prompt_tokens_details: _Details

    cc, cr = OpenAILLM._extract_cache_tokens(_U(_Details(789)))
    assert cc == 0  # OpenAI doesn't bill creation separately
    assert cr == 789


def test_b320_extract_cache_tokens_zeros_when_absent() -> None:
    """Provider with no cache stats at all — must not crash."""
    class _U: ...
    cc, cr = OpenAILLM._extract_cache_tokens(_U())
    assert (cc, cr) == (0, 0)


def test_b320_extract_cache_tokens_handles_none_usage() -> None:
    """Some compat shims omit usage entirely on certain responses."""
    cc, cr = OpenAILLM._extract_cache_tokens(None)
    assert (cc, cr) == (0, 0)


@pytest.mark.asyncio
async def test_b320_complete_surfaces_cache_tokens() -> None:
    """End-to-end: faked response with cache tokens → LLMResponse
    carries them on cache_creation_input_tokens / cache_read_input_tokens."""
    @dataclass
    class _CachedUsage:
        prompt_tokens: int
        completion_tokens: int
        cache_creation_input_tokens: int = 0
        cache_read_input_tokens: int = 0

    fake = _FakeResponse(
        choices=[_FakeChoice(message=_FakeMessage(content="ok"))],
        usage=_CachedUsage(
            prompt_tokens=100,
            completion_tokens=20,
            cache_creation_input_tokens=80,
            cache_read_input_tokens=900,
        ),
    )
    llm = OpenAILLM(api_key="x")
    llm._client = _FakeClient(fake)
    resp = await llm.complete([Message(role="user", content="hi")])
    assert resp.cache_creation_input_tokens == 80
    assert resp.cache_read_input_tokens == 900


# 2026-05-26: DeepSeek V4 thinking-mode echo-back


def test_thinking_echoed_as_content_block_and_top_level() -> None:
    """Assistant messages with ``thinking`` set must emit content as
    a block list with a ``thinking`` block AND a top-level
    ``reasoning_content`` field. DeepSeek V4 thinking mode 400s
    without this echo (``content[].thinking in the thinking mode
    must be passed back to the API``)."""
    msgs = OpenAILLM._messages_to_openai([
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content="Hello there.",
            thinking="The user said hi. I should reply warmly.",
        ),
        Message(role="user", content="and what now?"),
    ])
    # User messages unaffected.
    assert msgs[0]["content"] == "hi"
    assert msgs[2]["content"] == "and what now?"
    # Assistant entry has block-list content + top-level
    # reasoning_content.
    asst = msgs[1]
    assert isinstance(asst["content"], list)
    types = [b.get("type") for b in asst["content"]]
    assert "thinking" in types
    assert "text" in types
    thinking_block = next(b for b in asst["content"] if b["type"] == "thinking")
    assert "user said hi" in thinking_block["thinking"]
    assert asst["reasoning_content"] == thinking_block["thinking"]


def test_assistant_without_thinking_stays_plain_string() -> None:
    """Back-compat: assistant messages with no thinking field must
    keep emitting ``content`` as a plain string (the historical
    shape that every other test pins). No reasoning_content
    leakage on non-thinking turns."""
    msgs = OpenAILLM._messages_to_openai([
        Message(role="user", content="hi"),
        Message(role="assistant", content="Hello there."),
    ])
    asst = msgs[1]
    assert asst["content"] == "Hello there."
    assert "reasoning_content" not in asst


def test_assistant_thinking_with_tool_calls() -> None:
    """Assistant turn with BOTH thinking and tool calls — both must
    survive translation. (DeepSeek thinking-mode reasoning often
    precedes a tool call; if we drop either side the next hop
    400s or the tool dispatch breaks.)"""
    from xmclaw.core.ir import ToolCall, ToolCallShape
    tc = ToolCall(
        id="t1", name="bash", args={"command": "ls"},
        provenance=ToolCallShape.OPENAI_TOOL.value,
    )
    msgs = OpenAILLM._messages_to_openai([
        Message(role="user", content="run ls"),
        Message(
            role="assistant",
            content="",
            thinking="Plan: invoke bash with `ls`.",
            tool_calls=(tc,),
        ),
    ])
    asst = msgs[1]
    assert isinstance(asst["content"], list)
    assert any(b.get("type") == "thinking" for b in asst["content"])
    assert asst.get("tool_calls"), "tool_calls dropped"
    assert asst["reasoning_content"].startswith("Plan:")


def test_streaming_capture_records_thinking_in_response() -> None:
    """The streaming consume loop must accumulate reasoning_content
    chunks into ``LLMResponse.thinking`` so hop_loop can echo them
    on the next hop. Pre-fix the chunks were only fired into the
    on_thinking_chunk callback and discarded after that."""
    import asyncio
    from xmclaw.providers.llm.openai import OpenAILLM
    from xmclaw.providers.llm.base import LLMResponse

    class _FakeChunk:
        def __init__(self, content="", reasoning_content="", finish_reason=None):
            self.choices = [_FakeChoice(content, reasoning_content, finish_reason)]
            self.usage = None

    class _FakeChoice:
        def __init__(self, content, reasoning_content, finish_reason):
            self.delta = _FakeDelta(content, reasoning_content)
            self.finish_reason = finish_reason

    class _FakeDelta:
        def __init__(self, content, reasoning_content):
            self.content = content or None
            self.reasoning_content = reasoning_content or None
            self.tool_calls = None
        @property
        def model_extra(self):
            return {}

    class _FakeStream:
        def __init__(self, chunks):
            self._chunks = chunks
        def __aiter__(self):
            self._it = iter(self._chunks)
            return self
        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration
        async def aclose(self):
            return None

    class _FakeCompletions:
        async def create(self, **_):
            return _FakeStream([
                _FakeChunk(reasoning_content="reason A "),
                _FakeChunk(reasoning_content="reason B."),
                _FakeChunk(content="Hello, "),
                _FakeChunk(content="world.", finish_reason="stop"),
            ])

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    llm = OpenAILLM(api_key="x", model="deepseek-v4-pro")
    llm._client = _FakeClient()

    async def _run():
        return await llm.complete_streaming([Message(role="user", content="hi")])
    resp = asyncio.run(_run())
    assert isinstance(resp, LLMResponse)
    assert resp.thinking == "reason A reason B."
    assert resp.content == "Hello, world."
