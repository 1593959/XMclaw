"""OpenRouterLLM — construction, header injection, pricing, cache defaults.

B-386: OpenRouter as a 1st-class provider. Tests cover:

* Construction with explicit api_key works (no real network).
* base_url defaults to OpenRouter when not overridden; explicit
  override still wins.
* The lazy ``_get_client`` call attaches ``HTTP-Referer`` + ``X-Title``
  attribution headers via ``default_headers``.
* ``lookup_pricing`` resolves OpenRouter-style ``provider/model`` ids
  against the existing substring patterns — no parallel table needed.
* Prompt-cache auto-detect picks True for ``anthropic/`` and ``openai/``
  prefixes (matching the upstream's caching support), False otherwise.

All tests are offline — the ``AsyncOpenAI`` constructor is monkeypatched
to capture kwargs without a real client. The compat-test that exercises
``complete()`` reuses the same fake-client pattern as
``test_v2_openai_provider.py``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from xmclaw.providers.llm.base import Message, Pricing
from xmclaw.providers.llm.openai import OpenAILLM
from xmclaw.providers.llm.openrouter import (
    DEFAULT_BASE_URL,
    DEFAULT_HTTP_REFERER,
    DEFAULT_MODEL,
    DEFAULT_X_TITLE,
    OpenRouterLLM,
    _default_prompt_cache_for_openrouter,
)
from xmclaw.utils.cost import lookup_pricing, DEFAULT_FALLBACK_PRICING


# ── construction ─────────────────────────────────────────────────────────


def test_construction_with_explicit_api_key() -> None:
    """Bare construction works — no network call, no SDK import yet."""
    llm = OpenRouterLLM(api_key="sk-or-v1-test")
    assert llm.api_key == "sk-or-v1-test"
    # Inherits OpenAILLM (don't duplicate streaming logic).
    assert isinstance(llm, OpenAILLM)


def test_default_base_url_is_openrouter() -> None:
    """Without an explicit override, base_url points at openrouter.ai."""
    llm = OpenRouterLLM(api_key="sk-or-v1-test")
    assert llm.base_url == DEFAULT_BASE_URL
    assert llm.base_url == "https://openrouter.ai/api/v1"


def test_default_model_is_claude_sonnet_4() -> None:
    """Sensible default — Claude Sonnet 4 is the 2026-Q2 sweet spot."""
    llm = OpenRouterLLM(api_key="sk-or-v1-test")
    assert llm.model == DEFAULT_MODEL
    assert llm.model == "anthropic/claude-sonnet-4"


def test_explicit_base_url_overrides_default() -> None:
    """Self-hosted OpenRouter-compat gateway → user can point us at it."""
    llm = OpenRouterLLM(
        api_key="sk-or-v1-test",
        base_url="https://my-proxy.example.com/v1",
    )
    assert llm.base_url == "https://my-proxy.example.com/v1"


def test_explicit_empty_base_url_still_falls_back_to_default() -> None:
    """Factory may pass ``base_url=None`` when the config field is empty."""
    llm = OpenRouterLLM(api_key="sk-or-v1-test", base_url=None)
    assert llm.base_url == DEFAULT_BASE_URL


# ── header injection ─────────────────────────────────────────────────────


def test_get_client_passes_attribution_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lazy AsyncOpenAI construction must include HTTP-Referer +
    X-Title in default_headers — OpenRouter's recommended attribution.
    Without these the call appears as 'anonymous' on the dashboard."""
    captured_kwargs: dict[str, Any] = {}

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured_kwargs.update(kwargs)

    # Patch the openai SDK import so _get_client picks up the fake.
    import openai as _openai_mod
    monkeypatch.setattr(_openai_mod, "AsyncOpenAI", _FakeAsyncOpenAI)

    llm = OpenRouterLLM(api_key="sk-or-v1-test")
    llm._get_client()

    assert "default_headers" in captured_kwargs
    headers = captured_kwargs["default_headers"]
    assert headers["HTTP-Referer"] == DEFAULT_HTTP_REFERER
    assert headers["HTTP-Referer"] == "https://github.com/1593959/XMclaw"
    assert headers["X-Title"] == DEFAULT_X_TITLE
    assert headers["X-Title"] == "XMclaw"
    # Other kwargs we expect on the client.
    assert captured_kwargs["api_key"] == "sk-or-v1-test"
    assert captured_kwargs["base_url"] == DEFAULT_BASE_URL


def test_get_client_caches_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second call returns the same client — lazy construction, single
    SDK instance per provider object."""
    instance_count = 0

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            nonlocal instance_count
            instance_count += 1

    import openai as _openai_mod
    monkeypatch.setattr(_openai_mod, "AsyncOpenAI", _FakeAsyncOpenAI)

    llm = OpenRouterLLM(api_key="sk-or-v1-test")
    c1 = llm._get_client()
    c2 = llm._get_client()
    assert c1 is c2
    assert instance_count == 1


def test_custom_attribution_headers_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A power user / fork can pass their own attribution values."""
    captured_kwargs: dict[str, Any] = {}

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured_kwargs.update(kwargs)

    import openai as _openai_mod
    monkeypatch.setattr(_openai_mod, "AsyncOpenAI", _FakeAsyncOpenAI)

    llm = OpenRouterLLM(
        api_key="sk-or-v1-test",
        http_referer="https://example.com/myapp",
        x_title="MyForkOfXMclaw",
    )
    llm._get_client()
    assert captured_kwargs["default_headers"]["HTTP-Referer"] == "https://example.com/myapp"
    assert captured_kwargs["default_headers"]["X-Title"] == "MyForkOfXMclaw"


# ── pricing ──────────────────────────────────────────────────────────────


def test_pricing_for_default_model_resolves_to_claude_sonnet() -> None:
    """The substring matcher in lookup_pricing already finds
    ``claude-sonnet`` inside ``anthropic/claude-sonnet-4`` — no
    OpenRouter-specific table needed."""
    p = lookup_pricing(DEFAULT_MODEL)
    # claude-sonnet pattern is 3.0 / 15.0 — neither zero nor the
    # default fallback (0.5 / 1.5).
    assert p.input_per_mtok == 3.0
    assert p.output_per_mtok == 15.0
    assert p != DEFAULT_FALLBACK_PRICING


@pytest.mark.parametrize("model, expected_in, expected_out", [
    # Anthropic family via OpenRouter — claude-sonnet / haiku / opus
    # patterns all hit through substring match.
    ("anthropic/claude-sonnet-4",   3.0,  15.0),
    ("anthropic/claude-haiku-4-5",  0.8,   4.0),
    ("anthropic/claude-opus-4-7",  15.0,  75.0),
    # OpenAI family via OpenRouter.
    ("openai/gpt-4o",               2.5,  10.0),
    ("openai/gpt-4o-mini",          0.15,  0.6),
    # Open-weight families that already have substring patterns.
    ("meta-llama/llama-3.1-70b",    0.2,   0.6),
    ("qwen/qwen-2.5-72b",           0.3,   1.2),
    ("deepseek/deepseek-chat",      0.14,  0.28),
])
def test_pricing_resolves_for_openrouter_model_ids(
    model: str, expected_in: float, expected_out: float,
) -> None:
    """OpenRouter's ``<provider>/<model>`` ids round-trip through the
    existing substring-matching pricing table — neither zero nor the
    default fallback for any of the well-known families."""
    p = lookup_pricing(model)
    assert p.input_per_mtok == expected_in
    assert p.output_per_mtok == expected_out
    assert p != DEFAULT_FALLBACK_PRICING


def test_pricing_property_delegates_to_lookup() -> None:
    """OpenRouterLLM.pricing inherits OpenAILLM's behaviour: explicit
    override wins, otherwise lookup_pricing(self.model)."""
    llm = OpenRouterLLM(api_key="x")
    p = llm.pricing
    assert p.input_per_mtok == 3.0  # claude-sonnet pattern
    assert p.output_per_mtok == 15.0


def test_pricing_explicit_override_wins() -> None:
    custom = Pricing(input_per_mtok=999.0, output_per_mtok=1234.0)
    llm = OpenRouterLLM(api_key="x", pricing=custom)
    assert llm.pricing == custom


# ── prompt-cache auto-detect ─────────────────────────────────────────────


@pytest.mark.parametrize("model, expected", [
    # Anthropic prefix → on (Anthropic's native prompt caching pass-
    # through is well-supported on OpenRouter).
    ("anthropic/claude-sonnet-4",   True),
    ("anthropic/claude-haiku-4-5",  True),
    ("anthropic/claude-opus-4-7",   True),
    # OpenAI prefix → on (auto-caching, marker is harmless).
    ("openai/gpt-4o",               True),
    ("openai/gpt-4o-mini",          True),
    # Everything else → off (third-party shims may reject unknown
    # body fields).
    ("meta-llama/llama-3.1-70b",    False),
    ("qwen/qwen-2.5-72b",           False),
    ("deepseek/deepseek-chat",      False),
    ("google/gemini-pro-1.5",       False),
    ("",                            False),
])
def test_default_prompt_cache_picks_by_prefix(model: str, expected: bool) -> None:
    assert _default_prompt_cache_for_openrouter(model) is expected


def test_constructor_inherits_cache_for_anthropic_routed_model() -> None:
    """End-to-end: anthropic-routed model → cache enabled by default."""
    llm = OpenRouterLLM(api_key="x", model="anthropic/claude-sonnet-4")
    assert llm._prompt_cache_enabled is True


def test_constructor_inherits_cache_for_openai_routed_model() -> None:
    llm = OpenRouterLLM(api_key="x", model="openai/gpt-4o")
    assert llm._prompt_cache_enabled is True


def test_constructor_disables_cache_for_unknown_routed_model() -> None:
    """Unknown upstream → conservative default OFF."""
    llm = OpenRouterLLM(api_key="x", model="meta-llama/llama-3.1-70b")
    assert llm._prompt_cache_enabled is False


def test_constructor_explicit_override_wins() -> None:
    """User who knows their upstream's posture can force the marker."""
    llm = OpenRouterLLM(
        api_key="x",
        model="meta-llama/llama-3.1-70b",
        prompt_cache_enabled=True,
    )
    assert llm._prompt_cache_enabled is True

    llm2 = OpenRouterLLM(
        api_key="x",
        model="anthropic/claude-sonnet-4",
        prompt_cache_enabled=False,
    )
    assert llm2._prompt_cache_enabled is False


# ── inherited behaviour smoke-test ───────────────────────────────────────


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
        self.last_kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.last_kwargs = kwargs
        return self._response


class _FakeChatAPI:
    def __init__(self, response: _FakeResponse) -> None:
        self.completions = _FakeChatCompletionsAPI(response)


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.chat = _FakeChatAPI(response)


@pytest.mark.asyncio
async def test_complete_inherits_openai_streaming_logic() -> None:
    """Sanity check — OpenRouterLLM reuses OpenAILLM.complete() and
    decodes tool calls + usage tokens identically. We're not retesting
    the parent's behaviour exhaustively; this is a smoke test that
    subclassing didn't accidentally break anything."""
    fake = _FakeResponse(
        choices=[_FakeChoice(
            message=_FakeMessage(
                content="routed via OpenRouter",
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
        usage=_FakeUsage(prompt_tokens=10, completion_tokens=5),
    )
    llm = OpenRouterLLM(api_key="sk-or-v1-test")
    llm._client = _FakeClient(fake)
    resp = await llm.complete([Message(role="user", content="x")])
    assert resp.content == "routed via OpenRouter"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "file_read"
    assert resp.tool_calls[0].args == {"path": "/tmp/x"}
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens == 5


@pytest.mark.asyncio
async def test_complete_uses_openrouter_model_in_request() -> None:
    """The model id passed to the upstream chat.completions.create is
    the OpenRouter ``<provider>/<model>`` form, not just the bare
    upstream name."""
    fake = _FakeResponse(
        choices=[_FakeChoice(message=_FakeMessage(content="ok"))],
        usage=_FakeUsage(prompt_tokens=1, completion_tokens=1),
    )
    llm = OpenRouterLLM(
        api_key="sk-or-v1-test",
        model="anthropic/claude-sonnet-4",
    )
    fake_client = _FakeClient(fake)
    llm._client = fake_client
    await llm.complete([Message(role="user", content="x")])
    assert fake_client.chat.completions.last_kwargs["model"] == (
        "anthropic/claude-sonnet-4"
    )


# ── factory wiring ───────────────────────────────────────────────────────


def test_factory_recognises_openrouter_provider_key() -> None:
    """B-386: ``llm.openrouter`` block in config produces an
    OpenRouterLLM via the legacy build path."""
    from xmclaw.daemon.factory import build_llm_from_config

    llm = build_llm_from_config({
        "llm": {
            "openrouter": {
                "api_key": "sk-or-v1-fromcfg",
                "default_model": "openai/gpt-4o",
            },
        },
    })
    assert isinstance(llm, OpenRouterLLM)
    assert llm.api_key == "sk-or-v1-fromcfg"
    assert llm.model == "openai/gpt-4o"
    # Default base_url applied since config didn't override.
    assert llm.base_url == DEFAULT_BASE_URL


def test_factory_openrouter_uses_default_model_when_omitted() -> None:
    from xmclaw.daemon.factory import build_llm_from_config

    llm = build_llm_from_config({
        "llm": {"openrouter": {"api_key": "sk-or-v1-x"}},
    })
    assert isinstance(llm, OpenRouterLLM)
    assert llm.model == "anthropic/claude-sonnet-4"


def test_factory_anthropic_still_preferred_over_openrouter() -> None:
    """Both keys present → native Anthropic wins (cheaper + lower
    latency than going through OpenRouter)."""
    from xmclaw.daemon.factory import build_llm_from_config
    from xmclaw.providers.llm.anthropic import AnthropicLLM

    llm = build_llm_from_config({
        "llm": {
            "anthropic":  {"api_key": "sk-ant-key", "default_model": "claude"},
            "openrouter": {"api_key": "sk-or-v1-key"},
        },
    })
    assert isinstance(llm, AnthropicLLM)


def test_factory_falls_back_to_openrouter_when_native_keys_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both native blocks empty + openrouter set → factory picks
    OpenRouter. Mock the secrets layer because a real dev machine may
    have llm.anthropic.api_key / llm.openai.api_key in keyring or env,
    and the factory's secret-fallback would otherwise resolve those
    before reaching the openrouter block."""
    from xmclaw.daemon import factory as _factory_mod

    monkeypatch.setattr(
        _factory_mod, "get_secret",
        lambda _name: None,
        raising=False,
    )
    # ``build_llm_from_config`` does ``from xmclaw.utils.secrets import
    # get_secret`` inside the function, so patch the source module too.
    import xmclaw.utils.secrets as _secrets_mod
    monkeypatch.setattr(_secrets_mod, "get_secret", lambda _name: None)

    llm = _factory_mod.build_llm_from_config({
        "llm": {
            "anthropic":  {"api_key": ""},
            "openai":     {"api_key": ""},
            "openrouter": {"api_key": "sk-or-v1-fallback"},
        },
    })
    assert isinstance(llm, OpenRouterLLM)


def test_factory_profile_recognises_openrouter() -> None:
    """B-386: ``profiles[]`` entries with provider='openrouter' build
    a dedicated OpenRouterLLM (not a generic OpenAILLM with a
    fiddled base_url)."""
    from xmclaw.daemon.factory import build_llm_profiles_from_config

    profs = build_llm_profiles_from_config({
        "llm": {
            "profiles": [
                {
                    "id": "via-openrouter",
                    "label": "Via OpenRouter",
                    "provider": "openrouter",
                    "model": "openai/gpt-4o-mini",
                    "api_key": "sk-or-v1-prof",
                },
            ],
        },
    })
    assert len(profs) == 1
    assert profs[0].id == "via-openrouter"
    assert profs[0].provider_name == "openrouter"
    assert isinstance(profs[0].llm, OpenRouterLLM)
    assert profs[0].llm.model == "openai/gpt-4o-mini"
