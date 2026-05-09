"""Provider profile registry — preset shape for OpenAI-compat endpoints (B-387).

Anti-req #14 (protocol compat): :class:`OpenAILLM` already accepts any
``base_url`` so users CAN already point it at DeepSeek / Kimi / Qwen /
Gemini today. Problem: they have to know the exact base_url + suitable
default model + which capability flags the endpoint actually supports.
This registry centralises those defaults so downstream surfaces (CLI
``onboard`` wizard, ``xmclaw doctor``, future "preset picker" UI) can
offer named choices instead of asking for raw URLs.

The profiles only describe **wire-shape compat with OpenAILLM**. Native
providers (Anthropic, OpenRouter) live in their own adapters and are
not represented here.

Empty fields fall back to ``OpenAILLM`` defaults — adding a profile
never *changes* runtime behaviour, only documents what the endpoint
historically accepts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """Static metadata for one OpenAI-compat endpoint.

    Attributes
    ----------
    provider_id : str
        Stable id used as a config key (``llm.<provider_id>.api_key``,
        secret name ``llm.<provider_id>.api_key`` for keyring).
    display_name : str
        Human-readable label for pickers (CLI / future UI).
    default_base_url : str
        ``base_url`` to pass to :class:`OpenAILLM`. None of these are
        the SDK's own default — that one is OpenAI proper, which has
        no profile entry (an explicit "openai" profile would shadow
        the SDK default and is unnecessary).
    default_models : tuple[str, ...]
        Recommended chat-completion model ids in priority order. The
        first entry is what the wizard pre-selects; the rest are
        offered as alternatives. Empty tuple = let the user type one.
    default_max_tokens : int
        Conservative completion-token cap for the default model. Used
        by the wizard as a starting value, not enforced here.
    supports_thinking : bool
        Whether the endpoint emits ``reasoning_content`` /
        ``reasoning`` deltas in the streaming response. Profiles that
        set this True will pre-enable the AgentLoop's
        ``on_thinking_chunk`` wiring so the user sees the reasoning
        stream from the first turn (no manual config flip).
    supports_tool_use_streaming : bool
        Whether the endpoint streams partial ``tool_calls[i].arguments``
        in the SSE body (vs. only delivering tool_calls in the final
        chunk). All four profiled providers support this in 2026 — the
        flag exists for future endpoints that don't.
    supports_prompt_cache_marker : bool
        Whether the endpoint honours the Anthropic-style
        ``cache_control: {"type": "ephemeral"}`` marker on the OpenAI
        compat shim (B-320). Mirrors the same allowlist that
        :func:`xmclaw.providers.llm.openai._default_prompt_cache_enabled`
        uses today; centralising it here lets a future doctor check
        validate the two stay in sync.
    docs_url : str
        Link surfaced in error messages / wizard help text.
    """

    provider_id: str
    display_name: str
    default_base_url: str
    default_models: tuple[str, ...] = field(default_factory=tuple)
    default_max_tokens: int = 4096
    supports_thinking: bool = False
    supports_tool_use_streaming: bool = True
    supports_prompt_cache_marker: bool = False
    docs_url: str = ""


# ── canonical profiles (B-387) ────────────────────────────────────────

DEEPSEEK: Final = ProviderProfile(
    provider_id="deepseek",
    display_name="DeepSeek",
    default_base_url="https://api.deepseek.com/v1",
    default_models=("deepseek-chat", "deepseek-reasoner"),
    default_max_tokens=8192,
    # deepseek-reasoner emits reasoning_content; deepseek-chat does not.
    # Flag set True so the streaming wiring lights up for whichever
    # model the user picks; harmless on chat models (they just never
    # send the field).
    supports_thinking=True,
    supports_tool_use_streaming=True,
    # DeepSeek does AUTOMATIC caching (no opt-in) and reports hits via
    # ``prompt_tokens_details.cached_tokens`` — sending the
    # cache_control marker is a no-op at best and a 400 at worst, so
    # leave the marker off.
    supports_prompt_cache_marker=False,
    docs_url="https://api-docs.deepseek.com/api/create-chat-completion",
)


KIMI: Final = ProviderProfile(
    provider_id="kimi",
    display_name="Kimi (Moonshot)",
    default_base_url="https://api.moonshot.cn/v1",
    default_models=(
        "kimi-k2-0905-preview",
        "moonshot-v1-128k",
        "moonshot-v1-32k",
    ),
    default_max_tokens=8192,
    supports_thinking=True,  # K2 family streams reasoning_content.
    supports_tool_use_streaming=True,
    # Moonshot's compat shim DOES honour the cache_control marker
    # (B-320). The allowlist in openai.py picks this up via base_url
    # substring match — keeping the flag here lets a future
    # consistency check fire if either side drifts.
    supports_prompt_cache_marker=True,
    docs_url="https://platform.moonshot.cn/docs/api/chat",
)


QWEN: Final = ProviderProfile(
    provider_id="qwen",
    display_name="Qwen (通义千问 / DashScope)",
    # Alibaba's OpenAI-compat endpoint. The DashScope-native endpoint
    # is at .../api/v1/services/aigc/text-generation/generation but
    # XMclaw uses the compat shim because OpenAILLM speaks that wire.
    default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    default_models=(
        "qwen-plus",
        "qwen-max",
        "qwen-turbo",
        "qwen3-coder-plus",
    ),
    default_max_tokens=8192,
    # Qwen3 / QwQ stream reasoning_content; Qwen-Plus / Max / Turbo do
    # not — same trade-off as DeepSeek: enable the wiring, harmless on
    # non-reasoning models.
    supports_thinking=True,
    supports_tool_use_streaming=True,
    # DashScope's compat shim does NOT advertise cache_control support.
    # It does report cache hits via prompt_tokens_details.cached_tokens
    # (auto-caching), captured by _extract_cache_tokens already.
    supports_prompt_cache_marker=False,
    docs_url=(
        "https://help.aliyun.com/zh/model-studio/"
        "qwen-api-via-openai-chat-completions"
    ),
)


GEMINI: Final = ProviderProfile(
    provider_id="gemini",
    display_name="Google Gemini (OpenAI-compat)",
    # Google publishes an OpenAI-compat shim alongside the native
    # GenerativeLanguage REST API; this is the former.
    default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    default_models=(
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-3-flash-preview",
    ),
    default_max_tokens=8192,
    # Gemini 2.5 Flash / Pro emit thinking blocks via the native API;
    # the OpenAI-compat shim documents `reasoning` deltas in
    # streaming. Toggle on so the streaming wiring picks them up.
    supports_thinking=True,
    supports_tool_use_streaming=True,
    # Gemini's compat shim does NOT honour cache_control (Google has
    # its own context-caching API on the native side).
    supports_prompt_cache_marker=False,
    docs_url="https://ai.google.dev/gemini-api/docs/openai",
)


# Public registry — ordered tuple so wizard pickers stay stable.
PROFILES: Final[tuple[ProviderProfile, ...]] = (
    DEEPSEEK,
    KIMI,
    QWEN,
    GEMINI,
)


_BY_ID: Final[dict[str, ProviderProfile]] = {p.provider_id: p for p in PROFILES}


def get_profile(provider_id: str) -> ProviderProfile | None:
    """Return the profile registered under ``provider_id``, or None.

    Lookup is case-insensitive on ``provider_id``. Unknown ids return
    ``None`` so callers can fall through to a "free-form base_url"
    branch instead of crashing — important for self-hosted vLLM /
    Ollama / LiteLLM users who don't fit any preset.
    """
    if not isinstance(provider_id, str) or not provider_id:
        return None
    return _BY_ID.get(provider_id.lower())


def list_profiles() -> tuple[ProviderProfile, ...]:
    """Return the canonical, ordered tuple of profiles.

    Stable ordering (matches the source order of :data:`PROFILES`) so
    wizard menus don't shuffle between runs and CLI output is diff-able.
    """
    return PROFILES


def detect_profile_from_base_url(base_url: str | None) -> ProviderProfile | None:
    """Best-effort reverse lookup: given a ``base_url``, return the
    profile that matches.

    Used by ``xmclaw doctor`` to emit "looks like you're using DeepSeek;
    suggested defaults for that provider are X / Y" advisories without
    requiring the user to re-run onboard. Match is by host substring
    on the registered ``default_base_url`` — robust to ``/v1`` /
    ``/v1/`` / ``/openai/v1`` variations.
    """
    if not isinstance(base_url, str) or not base_url:
        return None
    lowered = base_url.lower()
    for profile in PROFILES:
        # Pull just the netloc-ish chunk out of the registered URL so
        # ``api.deepseek.com`` matches both ``…/v1`` and ``…/v1/`` and
        # ``…/v1/chat/completions``. Splitting on the first slash after
        # ``://`` is enough — we don't need a real urlparse here.
        anchor = profile.default_base_url.lower()
        sep = anchor.find("://")
        if sep != -1:
            anchor = anchor[sep + 3:]
        host = anchor.split("/", 1)[0]
        if host and host in lowered:
            return profile
    return None


__all__ = [
    "DEEPSEEK",
    "GEMINI",
    "KIMI",
    "PROFILES",
    "QWEN",
    "ProviderProfile",
    "detect_profile_from_base_url",
    "get_profile",
    "list_profiles",
]
