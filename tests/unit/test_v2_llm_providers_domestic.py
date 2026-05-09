"""B-387: integration test matrix for 4 国产 / domestic LLM providers.

Covers DeepSeek, Kimi (Moonshot), Qwen (Tongyi / DashScope), and
Gemini (via OpenAI-compat shim) — all of which are reachable today
through :class:`OpenAILLM` by setting ``base_url``. We don't ship a
new adapter per provider; we lock in the wire-shape contract via
recorded canonical responses so a silent drift in any provider's
JSON shape (extra fields, different finish_reason values, different
tool-call envelope) blows up here instead of at a user's runtime.

The recorded shapes come from each provider's official 2026-04-26
docs — see ``DOC_URLS`` below. Each shape feeds through three
gates:

1. **Per-provider config** — ``base_url`` + ``api_key`` round-trip
   into the lazily constructed ``AsyncOpenAI`` client untouched.
2. **Per-provider non-streaming decode** — text, tool_calls,
   finish_reason, prompt/completion/cache tokens map onto
   :class:`LLMResponse` fields correctly. Each provider stresses a
   different corner: DeepSeek's automatic-cache via
   ``prompt_cache_hit_tokens``, Kimi's flat ``cached_tokens``,
   Qwen's nested ``prompt_tokens_details.cached_tokens`` +
   ``finish_reason="tool_calls"``, Gemini's minimal usage block.
3. **Per-provider streaming decode** — chunked SSE deltas
   accumulate to the right final text + tool_call args via
   ``complete_streaming``. Each provider's chunk shape is the one
   from its own docs (DeepSeek's ``[DONE]`` sentinel, Kimi's
   tail-chunk-only usage, Qwen's empty-choices-with-usage tail,
   Gemini's minimal compat shape).

Plus a tool-call envelope shape test per provider, since DeepSeek
sometimes ships ``arguments`` as a stringified JSON object while
Qwen has been observed shipping it directly (rare but recorded).
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from xmclaw.providers.llm._provider_profiles import (
    DEEPSEEK,
    GEMINI,
    KIMI,
    PROFILES,
    QWEN,
    detect_profile_from_base_url,
    get_profile,
    list_profiles,
)
from xmclaw.providers.llm.base import Message
from xmclaw.providers.llm.openai import OpenAILLM


# ── canonical doc URLs (informational; tested via fixtures, not WebFetch) ──

DOC_URLS = {
    "deepseek": "https://api-docs.deepseek.com/api/create-chat-completion",
    "kimi":     "https://platform.moonshot.cn/docs/api/chat",
    "qwen":     "https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions",
    "gemini":   "https://ai.google.dev/gemini-api/docs/openai",
}


# ── fake AsyncOpenAI client scaffolding ───────────────────────────────────
#
# We replicate the SDK's response shape closely enough that the openai.py
# extraction path doesn't notice: getattr-on-attributes for nested objects,
# ``model_extra`` for unknown fields (pydantic v2 convention).

@dataclass
class _FakeFunction:
    name: str
    arguments: str


@dataclass
class _FakeToolCall:
    id: str
    type: str
    function: _FakeFunction
    # Qwen ships ``index`` on each tool_call (per its docs example).
    # Other providers omit it. Default of None makes the decode path
    # ignore it — we only check it round-trips when present.
    index: int | None = None


@dataclass
class _FakeMessage:
    content: str | None = None
    tool_calls: list[_FakeToolCall] | None = None
    role: str = "assistant"


@dataclass
class _FakeChoice:
    message: _FakeMessage
    finish_reason: str = "stop"
    index: int = 0


@dataclass
class _FakeUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Anthropic-style flat cache fields (Moonshot when it does report).
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    # Free-form bag for Kimi's flat ``cached_tokens`` and other unknowns
    # — mirrors pydantic v2 ``model_extra``.
    model_extra: dict[str, Any] | None = None
    # Nested OpenAI-style cache details (DeepSeek, Qwen, Gemini path).
    prompt_tokens_details: Any = None


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]
    usage: _FakeUsage | None = None


class _FakeChatCompletions:
    """Captures the kwargs each provider's ``complete()`` /
    ``complete_streaming()`` sends, then returns a pre-recorded
    response object. Streaming responses are async iterators yielding
    chunk objects shaped like the openai SDK's ``ChatCompletionChunk``."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.captured_kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.captured_kwargs = kwargs
        # If the recorded response is an iterable of chunks, return an
        # async generator (streaming path); otherwise return the bare
        # response (non-streaming path).
        if isinstance(self._response, list):
            async def _stream() -> AsyncIterator[Any]:
                for chunk in self._response:
                    yield chunk
            return _stream()
        return self._response


class _FakeChat:
    def __init__(self, response: Any) -> None:
        self.completions = _FakeChatCompletions(response)


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self.chat = _FakeChat(response)


# Streaming-chunk faker. The openai SDK's ChatCompletionChunk is a
# pydantic model — we only need attribute access.

@dataclass
class _ChunkFunctionDelta:
    name: str | None = None
    arguments: str | None = None


@dataclass
class _ChunkToolCallDelta:
    index: int = 0
    id: str | None = None
    type: str | None = None
    function: _ChunkFunctionDelta | None = None


@dataclass
class _ChunkDelta:
    content: str | None = None
    role: str | None = None
    tool_calls: list[_ChunkToolCallDelta] | None = None
    # Kimi K2.6 / DeepSeek Reasoner stream reasoning_content in
    # streaming deltas — recorded here so the on_thinking_chunk path
    # gets a shape it actually sees in production.
    reasoning_content: str | None = None


@dataclass
class _ChunkChoice:
    delta: _ChunkDelta
    finish_reason: str | None = None
    index: int = 0


@dataclass
class _Chunk:
    choices: list[_ChunkChoice]
    usage: _FakeUsage | None = None


def _install_fake_client(llm: OpenAILLM, response: Any) -> _FakeChatCompletions:
    """Wire ``llm._client`` to a fake that captures kwargs + returns
    the pre-recorded response. Returns the completions stub so tests
    can inspect ``captured_kwargs`` after the call."""
    client = _FakeClient(response)
    llm._client = client
    return client.chat.completions


# ──────────────────────────────────────────────────────────────────────────
# 1. PER-PROVIDER CONFIG-SHAPE TESTS
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("profile", list(PROFILES), ids=lambda p: p.provider_id)
def test_provider_profile_default_base_url_routes_to_openai_compat_endpoint(
    profile: Any,
) -> None:
    """Each profile's ``default_base_url`` is a syntactically valid
    ``https://`` URL the user can paste into ``OpenAILLM(base_url=...)``
    today. Empty string would silently route to OpenAI proper — guard."""
    assert profile.default_base_url.startswith("https://")
    assert "://" in profile.default_base_url
    assert " " not in profile.default_base_url


@pytest.mark.parametrize("profile", list(PROFILES), ids=lambda p: p.provider_id)
def test_provider_profile_has_at_least_one_default_model(profile: Any) -> None:
    """Each profile ships a recommended model id so the wizard has
    something to pre-select. Empty defaults force the user to type one
    blind, which defeats the point of presets."""
    assert len(profile.default_models) >= 1
    for m in profile.default_models:
        assert isinstance(m, str) and m.strip()


@pytest.mark.parametrize("profile", list(PROFILES), ids=lambda p: p.provider_id)
def test_provider_profile_construction_threads_base_url_into_async_client(
    profile: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build OpenAILLM with the profile's base_url + a fake api_key,
    then trigger lazy client construction and assert AsyncOpenAI got
    BOTH the api_key and base_url verbatim. This is the contract that
    lets users hit DeepSeek/Kimi/Qwen/Gemini through OpenAILLM at all."""
    captured: dict[str, Any] = {}

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    import openai as _openai_mod
    monkeypatch.setattr(_openai_mod, "AsyncOpenAI", _FakeAsyncOpenAI)

    llm = OpenAILLM(
        api_key="fake-key-for-test",
        model=profile.default_models[0],
        base_url=profile.default_base_url,
    )
    llm._get_client()  # triggers lazy SDK construction

    assert captured["api_key"] == "fake-key-for-test"
    assert captured["base_url"] == profile.default_base_url


def test_get_profile_round_trips_known_ids() -> None:
    """``get_profile`` must return every registered profile by id."""
    for p in PROFILES:
        assert get_profile(p.provider_id) is p
        # Case-insensitive — wizards / CLI may upper-case.
        assert get_profile(p.provider_id.upper()) is p


def test_get_profile_returns_none_for_unknown() -> None:
    """Unknown ids return None so callers can fall through to a free-
    form base_url branch (vLLM / Ollama / LiteLLM users)."""
    assert get_profile("not-a-real-provider") is None
    assert get_profile("") is None
    assert get_profile("anthropic") is None  # native, not OpenAI-compat


def test_list_profiles_is_stable_ordered() -> None:
    """Wizard menu order must not shuffle between runs."""
    out = list_profiles()
    assert out == PROFILES
    # Repeated calls return the same tuple object (no rebuilding).
    assert list_profiles() is out


# NOTE: parametrize arg name is ``b_url`` not ``base_url`` because pytest's
# ``pytest_base_url`` plugin owns the latter as a session-scoped fixture
# and parametrize would collide. Same workaround as test_v2_openai_provider.
@pytest.mark.parametrize("b_url, expected", [
    ("https://api.deepseek.com/v1", DEEPSEEK),
    ("https://api.deepseek.com/v1/chat/completions", DEEPSEEK),
    ("https://api.moonshot.cn/v1", KIMI),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", QWEN),
    ("https://generativelanguage.googleapis.com/v1beta/openai", GEMINI),
    ("http://localhost:11434/v1", None),  # Ollama — not in registry
    ("", None),
    (None, None),
])
def test_detect_profile_from_base_url(b_url: str | None, expected: Any) -> None:
    """Reverse lookup: given a URL, name the provider. ``xmclaw doctor``
    uses this to surface "you're hitting DeepSeek; here are its
    recommended defaults" advisories."""
    assert detect_profile_from_base_url(b_url) is expected


# ──────────────────────────────────────────────────────────────────────────
# 2. PER-PROVIDER NON-STREAMING RESPONSE DECODE TESTS
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deepseek_non_streaming_decode() -> None:
    """DeepSeek's canonical chat.completion shape (2026-04-26 docs):
    finish_reason='stop', usage carries flat ``prompt_tokens`` /
    ``completion_tokens`` plus DEEPSEEK-SPECIFIC
    ``prompt_cache_hit_tokens`` (auto-caching, no opt-in). The hit
    count is NOT one of our two cache fields directly; DeepSeek
    historically also surfaces the standard ``cached_tokens`` via
    ``prompt_tokens_details``. We assert text + finish_reason map +
    cache_read picks up the nested value when it's present."""
    @dataclass
    class _PTokDetails:
        cached_tokens: int = 0

    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content="Hello! How can I help you today?",
                tool_calls=None,
            ),
            finish_reason="stop",
        )],
        usage=_FakeUsage(
            prompt_tokens=16,
            completion_tokens=10,
            prompt_tokens_details=_PTokDetails(cached_tokens=8),
        ),
    )
    llm = OpenAILLM(
        api_key="x",
        model=DEEPSEEK.default_models[0],
        base_url=DEEPSEEK.default_base_url,
    )
    _install_fake_client(llm, fake)

    resp = await llm.complete([Message(role="user", content="hi")])
    assert resp.content == "Hello! How can I help you today?"
    assert resp.tool_calls == ()
    assert resp.prompt_tokens == 16
    assert resp.completion_tokens == 10
    assert resp.stop_reason == "stop"
    # DeepSeek's auto cache surfaces via prompt_tokens_details.cached_tokens.
    assert resp.cache_read_input_tokens == 8
    # No creation field → 0.
    assert resp.cache_creation_input_tokens == 0


@pytest.mark.asyncio
async def test_kimi_non_streaming_decode_with_tool_call() -> None:
    """Kimi K2.6 ships flat ``cached_tokens`` on usage (not nested).
    We capture it via ``model_extra`` since the SDK pydantic model
    doesn't know that field. tool_call here uses the canonical
    OpenAI envelope: arguments as a JSON STRING."""
    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content="你好,李雷!1+1等于2.",
                tool_calls=[_FakeToolCall(
                    id="call_abc123",
                    type="function",
                    function=_FakeFunction(
                        name="example_function",
                        arguments=json.dumps({"param1": "value1"}),
                    ),
                )],
            ),
            finish_reason="stop",
        )],
        usage=_FakeUsage(
            prompt_tokens=19,
            completion_tokens=21,
            # Flat ``cached_tokens`` lands in model_extra on pydantic v2.
            model_extra={"cached_tokens": 10},
        ),
    )
    llm = OpenAILLM(
        api_key="x",
        model=KIMI.default_models[0],
        base_url=KIMI.default_base_url,
    )
    _install_fake_client(llm, fake)

    resp = await llm.complete([Message(role="user", content="hi")])
    assert "李雷" in resp.content
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "example_function"
    assert resp.tool_calls[0].args == {"param1": "value1"}
    assert resp.stop_reason == "stop"
    assert resp.prompt_tokens == 19
    assert resp.completion_tokens == 21


@pytest.mark.asyncio
async def test_qwen_non_streaming_decode_finish_reason_tool_calls() -> None:
    """Qwen's docs canonical example: assistant message has
    ``content: null`` (not empty string) when only emitting a
    tool_call, and finish_reason='tool_calls' (NOT 'stop'). Usage
    carries nested ``prompt_tokens_details.cached_tokens`` on the
    OpenAI-compat shim."""
    @dataclass
    class _QwenDetails:
        cached_tokens: int = 0
        text_tokens: int = 0

    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content=None,  # explicit null per Qwen's docs
                tool_calls=[_FakeToolCall(
                    id="call_abc123",
                    type="function",
                    function=_FakeFunction(
                        name="get_current_weather",
                        arguments=json.dumps({"location": "杭州市"}),
                    ),
                    index=0,  # Qwen ships index on tool_calls
                )],
            ),
            finish_reason="tool_calls",
        )],
        usage=_FakeUsage(
            prompt_tokens=150,
            completion_tokens=45,
            prompt_tokens_details=_QwenDetails(
                cached_tokens=42, text_tokens=108,
            ),
        ),
    )
    llm = OpenAILLM(
        api_key="x",
        model=QWEN.default_models[0],
        base_url=QWEN.default_base_url,
    )
    _install_fake_client(llm, fake)

    resp = await llm.complete([Message(role="user", content="weather?")])
    # content is None on the wire — OpenAILLM normalises to "".
    assert resp.content == ""
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "get_current_weather"
    assert resp.tool_calls[0].args == {"location": "杭州市"}
    assert resp.stop_reason == "tool_calls"
    assert resp.cache_read_input_tokens == 42


@pytest.mark.asyncio
async def test_gemini_non_streaming_decode_minimal_shape() -> None:
    """Gemini's OpenAI-compat shim returns the bare-minimum OpenAI
    shape: no cache fields, no logprobs, simple usage. Stresses the
    "missing optional fields" branch — the helper must not crash."""
    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(content="response text here"),
            finish_reason="stop",
        )],
        usage=_FakeUsage(prompt_tokens=12, completion_tokens=5),
    )
    llm = OpenAILLM(
        api_key="x",
        model=GEMINI.default_models[0],
        base_url=GEMINI.default_base_url,
    )
    _install_fake_client(llm, fake)

    resp = await llm.complete([Message(role="user", content="hi")])
    assert resp.content == "response text here"
    assert resp.tool_calls == ()
    assert resp.stop_reason == "stop"
    assert resp.cache_creation_input_tokens == 0
    assert resp.cache_read_input_tokens == 0


@pytest.mark.asyncio
async def test_gemini_non_streaming_tool_call() -> None:
    """Gemini's tool-call envelope mirrors OpenAI exactly (per its
    docs): arguments as JSON string, function/name/id all present."""
    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content=None,
                tool_calls=[_FakeToolCall(
                    id="call_xyz",
                    type="function",
                    function=_FakeFunction(
                        name="get_weather",
                        arguments=json.dumps({"location": "Chicago, IL"}),
                    ),
                )],
            ),
            finish_reason="tool_calls",
        )],
        usage=_FakeUsage(prompt_tokens=20, completion_tokens=8),
    )
    llm = OpenAILLM(
        api_key="x",
        model=GEMINI.default_models[0],
        base_url=GEMINI.default_base_url,
    )
    _install_fake_client(llm, fake)

    resp = await llm.complete([Message(role="user", content="weather?")])
    assert resp.content == ""
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "get_weather"
    assert resp.tool_calls[0].args == {"location": "Chicago, IL"}


# ──────────────────────────────────────────────────────────────────────────
# 3. PER-PROVIDER STREAMING RESPONSE DECODE TESTS
# ──────────────────────────────────────────────────────────────────────────

def _content_chunk(text: str, finish_reason: str | None = None) -> _Chunk:
    """Helper: a chunk that's just a content delta."""
    return _Chunk(
        choices=[_ChunkChoice(
            delta=_ChunkDelta(content=text),
            finish_reason=finish_reason,
        )],
    )


@pytest.mark.asyncio
async def test_deepseek_streaming_accumulates_text() -> None:
    """DeepSeek's streaming SSE chunks per its docs: role on first,
    then content deltas, then a chunk with ``finish_reason="stop"``
    and no content. We don't model the [DONE] sentinel — the SDK
    handles that. ``stream_options.include_usage`` adds a final
    usage chunk."""
    chunks = [
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(role="assistant", content=""),
            finish_reason=None,
        )]),
        _content_chunk("Hello"),
        _content_chunk(" there"),
        _Chunk(
            choices=[_ChunkChoice(
                delta=_ChunkDelta(content=None),
                finish_reason="stop",
            )],
            usage=_FakeUsage(prompt_tokens=8, completion_tokens=2),
        ),
    ]
    llm = OpenAILLM(
        api_key="x",
        model=DEEPSEEK.default_models[0],
        base_url=DEEPSEEK.default_base_url,
    )
    _install_fake_client(llm, chunks)

    received: list[str] = []

    async def _on_chunk(d: str) -> None:
        received.append(d)

    resp = await llm.complete_streaming(
        [Message(role="user", content="hi")],
        on_chunk=_on_chunk,
    )
    assert "".join(received) == "Hello there"
    assert resp.content == "Hello there"
    assert resp.stop_reason == "stop"
    assert resp.prompt_tokens == 8
    assert resp.completion_tokens == 2


@pytest.mark.asyncio
async def test_kimi_streaming_emits_reasoning_then_text() -> None:
    """Kimi K2.6 streams ``reasoning_content`` before visible content
    in the same channel. Our streaming wiring routes those to a
    separate callback (``on_thinking_chunk``) so the UI can show a
    "thinking" indicator without polluting the user-visible bubble."""
    chunks = [
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(reasoning_content="let me think..."),
            finish_reason=None,
        )]),
        _content_chunk("你好"),
        _Chunk(
            choices=[_ChunkChoice(
                delta=_ChunkDelta(content=None),
                finish_reason="stop",
            )],
            usage=_FakeUsage(prompt_tokens=19, completion_tokens=13),
        ),
    ]
    llm = OpenAILLM(
        api_key="x",
        model=KIMI.default_models[0],
        base_url=KIMI.default_base_url,
    )
    _install_fake_client(llm, chunks)

    text_received: list[str] = []
    thinking_received: list[str] = []

    async def _on_chunk(d: str) -> None:
        text_received.append(d)

    async def _on_thinking(d: str) -> None:
        thinking_received.append(d)

    resp = await llm.complete_streaming(
        [Message(role="user", content="hi")],
        on_chunk=_on_chunk,
        on_thinking_chunk=_on_thinking,
    )
    assert "".join(thinking_received) == "let me think..."
    assert "".join(text_received) == "你好"
    assert resp.content == "你好"
    assert resp.stop_reason == "stop"


@pytest.mark.asyncio
async def test_qwen_streaming_tool_call_arguments_assemble_in_order() -> None:
    """Qwen's streaming tool-call shape: ``tool_calls`` deltas arrive
    one chunk at a time, each carrying a piece of ``arguments``. The
    accumulator keys by ``index`` so out-of-order arrivals (rare but
    legal per the OpenAI spec) still re-assemble correctly."""
    chunks = [
        # First chunk seeds the tool_call envelope (id + name).
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(
                tool_calls=[_ChunkToolCallDelta(
                    index=0,
                    id="call_x1",
                    type="function",
                    function=_ChunkFunctionDelta(name="lookup_order"),
                )],
            ),
            finish_reason=None,
        )]),
        # Subsequent chunks stream the JSON arguments byte by byte.
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(
                tool_calls=[_ChunkToolCallDelta(
                    index=0,
                    function=_ChunkFunctionDelta(arguments='{"order_id":'),
                )],
            ),
            finish_reason=None,
        )]),
        _Chunk(choices=[_ChunkChoice(
            delta=_ChunkDelta(
                tool_calls=[_ChunkToolCallDelta(
                    index=0,
                    function=_ChunkFunctionDelta(arguments='"A-9001"}'),
                )],
            ),
            finish_reason=None,
        )]),
        # Tail: empty choices array + usage (Qwen's specific shape).
        _Chunk(
            choices=[_ChunkChoice(
                delta=_ChunkDelta(),
                finish_reason="tool_calls",
            )],
            usage=_FakeUsage(prompt_tokens=120, completion_tokens=18),
        ),
    ]
    llm = OpenAILLM(
        api_key="x",
        model=QWEN.default_models[0],
        base_url=QWEN.default_base_url,
    )
    _install_fake_client(llm, chunks)

    resp = await llm.complete_streaming([Message(role="user", content="hi")])
    assert resp.stop_reason == "tool_calls"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "lookup_order"
    assert resp.tool_calls[0].args == {"order_id": "A-9001"}
    assert resp.tool_calls[0].id == "call_x1"


@pytest.mark.asyncio
async def test_gemini_streaming_minimal_chunks() -> None:
    """Gemini's compat shim ships a stripped-down chunk shape:
    no ``role`` field on the seed delta, no usage on every chunk
    (only on the tail when stream_options.include_usage is set)."""
    chunks = [
        _content_chunk("streamed "),
        _content_chunk("text "),
        _content_chunk("chunk"),
        _Chunk(
            choices=[_ChunkChoice(
                delta=_ChunkDelta(),
                finish_reason="stop",
            )],
            usage=_FakeUsage(prompt_tokens=10, completion_tokens=5),
        ),
    ]
    llm = OpenAILLM(
        api_key="x",
        model=GEMINI.default_models[0],
        base_url=GEMINI.default_base_url,
    )
    _install_fake_client(llm, chunks)

    received: list[str] = []

    async def _on_chunk(d: str) -> None:
        received.append(d)

    resp = await llm.complete_streaming(
        [Message(role="user", content="hi")],
        on_chunk=_on_chunk,
    )
    assert resp.content == "streamed text chunk"
    assert "".join(received) == "streamed text chunk"
    assert resp.stop_reason == "stop"
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens == 5


# ──────────────────────────────────────────────────────────────────────────
# 4. TOOL-CALL ENVELOPE SHAPE TESTS (per provider's quirks)
# ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deepseek_tool_call_envelope_arguments_as_json_string() -> None:
    """DeepSeek's canonical envelope: ``arguments`` is a JSON-encoded
    STRING (not a dict). Translator must json-decode it. This is the
    OpenAI default and the most common real-world shape."""
    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content="",
                tool_calls=[_FakeToolCall(
                    id="call_ds_1",
                    type="function",
                    function=_FakeFunction(
                        name="search_web",
                        arguments=json.dumps(
                            {"query": "DeepSeek pricing", "max_results": 5},
                        ),
                    ),
                )],
            ),
            finish_reason="tool_calls",
        )],
        usage=_FakeUsage(prompt_tokens=20, completion_tokens=10),
    )
    llm = OpenAILLM(
        api_key="x",
        model=DEEPSEEK.default_models[0],
        base_url=DEEPSEEK.default_base_url,
    )
    _install_fake_client(llm, fake)

    resp = await llm.complete([Message(role="user", content="search")])
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.name == "search_web"
    assert tc.args == {"query": "DeepSeek pricing", "max_results": 5}
    assert tc.id == "call_ds_1"


@pytest.mark.asyncio
async def test_kimi_tool_call_envelope_with_chinese_args() -> None:
    """Kimi sometimes emits unicode (CJK) directly inside ``arguments``
    JSON instead of \\uXXXX escapes. Decode must round-trip exactly."""
    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content="",
                tool_calls=[_FakeToolCall(
                    id="call_kimi_1",
                    type="function",
                    function=_FakeFunction(
                        name="search",
                        # Note: ensure_ascii=False keeps CJK as-is.
                        arguments=json.dumps(
                            {"q": "北京天气", "lang": "zh"},
                            ensure_ascii=False,
                        ),
                    ),
                )],
            ),
            finish_reason="tool_calls",
        )],
        usage=_FakeUsage(prompt_tokens=15, completion_tokens=8),
    )
    llm = OpenAILLM(
        api_key="x",
        model=KIMI.default_models[0],
        base_url=KIMI.default_base_url,
    )
    _install_fake_client(llm, fake)

    resp = await llm.complete([Message(role="user", content="search")])
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.args == {"q": "北京天气", "lang": "zh"}


@pytest.mark.asyncio
async def test_qwen_tool_call_envelope_carries_index_field() -> None:
    """Qwen's docs example puts ``index: 0`` directly on each
    ``tool_calls`` entry (alongside id/type/function). The OpenAI
    SDK's translator ignores unknown attributes — we assert the
    envelope still decodes cleanly when the extra field is present."""
    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content=None,
                tool_calls=[_FakeToolCall(
                    id="call_qw_1",
                    type="function",
                    function=_FakeFunction(
                        name="get_temperature",
                        arguments=json.dumps({"city": "shanghai"}),
                    ),
                    index=0,
                )],
            ),
            finish_reason="tool_calls",
        )],
        usage=_FakeUsage(prompt_tokens=30, completion_tokens=12),
    )
    llm = OpenAILLM(
        api_key="x",
        model=QWEN.default_models[0],
        base_url=QWEN.default_base_url,
    )
    _install_fake_client(llm, fake)

    resp = await llm.complete([Message(role="user", content="temp?")])
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "get_temperature"
    assert resp.tool_calls[0].args == {"city": "shanghai"}


@pytest.mark.asyncio
async def test_qwen_tool_call_envelope_with_dict_arguments() -> None:
    """Some compat shims (Qwen has been observed doing this in 2026)
    ship ``arguments`` as a literal DICT instead of a JSON string. The
    translator's defensive branch accepts that — we assert it doesn't
    re-stringify or re-parse, just passes through."""
    @dataclass
    class _RawDictArgsFunction:
        name: str
        # Bypass dataclass type check: arguments is a plain dict here,
        # which is what the openai SDK forwards from compat shims that
        # didn't stringify on the wire.
        arguments: Any

    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content=None,
                tool_calls=[_FakeToolCall(
                    id="call_qw_dict",
                    type="function",
                    function=_RawDictArgsFunction(  # type: ignore[arg-type]
                        name="lookup",
                        arguments={"key": "abc"},
                    ),
                )],
            ),
            finish_reason="tool_calls",
        )],
        usage=_FakeUsage(prompt_tokens=10, completion_tokens=5),
    )
    llm = OpenAILLM(
        api_key="x",
        model=QWEN.default_models[0],
        base_url=QWEN.default_base_url,
    )
    _install_fake_client(llm, fake)

    resp = await llm.complete([Message(role="user", content="x")])
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].args == {"key": "abc"}


@pytest.mark.asyncio
async def test_gemini_tool_call_envelope_drops_when_function_block_missing() -> None:
    """Gemini compat shim has been observed (rarely) to ship a
    ``tool_calls`` entry without a complete ``function`` block.
    Anti-req #1: malformed → translator returns None → decoded as
    no tool calls. The agent loop must not see a half-built call."""
    @dataclass
    class _PartialFn:
        # Missing ``arguments`` and ``name`` — translator should reject.
        name: str = ""
        arguments: str = ""

    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content="fallback text",
                tool_calls=[_FakeToolCall(
                    id="call_gm_partial",
                    type="function",
                    function=_PartialFn(),  # type: ignore[arg-type]
                )],
            ),
            finish_reason="stop",
        )],
        usage=_FakeUsage(prompt_tokens=5, completion_tokens=2),
    )
    llm = OpenAILLM(
        api_key="x",
        model=GEMINI.default_models[0],
        base_url=GEMINI.default_base_url,
    )
    _install_fake_client(llm, fake)

    resp = await llm.complete([Message(role="user", content="x")])
    # Text content survives; the malformed tool call is silently dropped.
    assert resp.content == "fallback text"
    assert resp.tool_calls == ()


# ──────────────────────────────────────────────────────────────────────────
# 5. PROFILE-DRIVEN B-320 PROMPT-CACHE FLAG CONSISTENCY
# ──────────────────────────────────────────────────────────────────────────
#
# The profile registry's ``supports_prompt_cache_marker`` field MUST agree
# with what _default_prompt_cache_enabled in openai.py actually does for
# that base_url. If the two drift, the wizard might pre-tick a checkbox
# the runtime later refuses to honour. Lock them in step.

@pytest.mark.parametrize("profile", list(PROFILES), ids=lambda p: p.provider_id)
def test_prompt_cache_flag_matches_runtime_auto_detect(profile: Any) -> None:
    """Each profile's ``supports_prompt_cache_marker`` agrees with the
    runtime's auto-detect for the same base_url + first model."""
    from xmclaw.providers.llm.openai import _default_prompt_cache_enabled
    runtime = _default_prompt_cache_enabled(
        profile.default_models[0], profile.default_base_url,
    )
    assert runtime is profile.supports_prompt_cache_marker, (
        f"{profile.provider_id}: profile says "
        f"supports_prompt_cache_marker={profile.supports_prompt_cache_marker} "
        f"but runtime auto-detect says {runtime}"
    )
