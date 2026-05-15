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
    evolution_tier : str
        Sprint 3 Iron Rule #3 — capability tier for the evolution loop.
        ``"strong"`` (Claude Opus / Sonnet 4 / GPT-4o / DeepSeek-V3 /
        Kimi-K2): full self-extension prompts, reflective mutation,
        strategy distillation. ``"medium"`` (DeepSeek-R1 distill-32B /
        Qwen-Plus / Gemini-2.5-Flash): can do strategy distillation +
        constrained mutation; self-extension prompts get
        template-fill scaffolding. ``"weak"`` (7B-class local models,
        GPT-5 the actual model not the marketing one): skip distill +
        mutation entirely — silent noise generators. ``"unknown"``:
        treat as ``"medium"``. Honest disclosure: tier is a heuristic
        based on Live-SWE-agent issue #7 + community feedback, not a
        formal benchmark — a model's actual ability varies by task.
    supports_self_extension : bool
        Iron Rule #3: when True, the agent_loop's step-reflection
        prompt asks for new tool / skill source. When False, the
        prompt asks for a template-fill (pick from existing skills)
        instead. Live-SWE-agent issue #7: GPT-5 / GPT-5-mini under
        the same prompt do NOT actually synthesize tools while
        Claude Sonnet 4.5 does — the only honest way to ship the
        feature is per-model gating.
    supports_reflective_mutation : bool
        Iron Rule #3: when True, ReflectiveMutator (Sprint 3 #2) is
        called with the model. When False, the mutator is skipped
        (no mutation candidates produced; HEAD survives). Saves the
        round-trip on weak models that produce noise instead of
        useful mutations.
    supports_strategy_distillation : bool
        Iron Rule #3: when True, StrategyDistiller (Sprint 3 #6) is
        called with the model. When False, no strategies get
        distilled — strategy_bank stays empty for that user. Same
        rationale as ``supports_reflective_mutation``: weak models'
        output is statistically common phrases, not useful patterns.
    docs_url : str
        Link surfaced in error messages / wizard help text.
    context_length : int
        Default context-window size in tokens for the *first* entry in
        ``default_models``. Wave-26 compression overhaul: the compressor
        used to hardcode 200K for every model, which meant a 256K
        Kimi-K2 session compressed at 39% of its real capacity. With
        per-profile declaration + the model-pattern table below, the
        threshold gate fires at the right place for each model. Falls
        back to 200K when unknown.
    """

    provider_id: str
    display_name: str
    default_base_url: str
    default_models: tuple[str, ...] = field(default_factory=tuple)
    default_max_tokens: int = 4096
    context_length: int = 200_000
    supports_thinking: bool = False
    supports_tool_use_streaming: bool = True
    supports_prompt_cache_marker: bool = False
    # Sprint 3 Iron Rule #3 — evolution tier + 3 capability flags.
    # Defaults are conservative ("medium" + all three on) so a brand-
    # new profile entry behaves as if it MIGHT support evolution
    # surfaces, with the option for an operator to flip individual
    # flags off via config override (deferred follow-up — for now
    # the registry value wins).
    evolution_tier: str = "medium"
    supports_self_extension: bool = True
    supports_reflective_mutation: bool = True
    supports_strategy_distillation: bool = True
    docs_url: str = ""


# ── canonical profiles (B-387) ────────────────────────────────────────

DEEPSEEK: Final = ProviderProfile(
    provider_id="deepseek",
    display_name="DeepSeek",
    default_base_url="https://api.deepseek.com/v1",
    default_models=("deepseek-chat", "deepseek-reasoner"),
    default_max_tokens=8192,
    # deepseek-chat / deepseek-reasoner: 64K context (Jan 2026 docs).
    context_length=64_000,
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
    # Sprint 3 Iron Rule #3: DeepSeek-V3 / R1 are strong on
    # synthesis tasks; community + benchmarks place them in the same
    # league as GPT-4o for tool synthesis.
    evolution_tier="strong",
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
    # K2-0905 ships with a 256K context. The moonshot-v1-* aliases each
    # declare their size in the name and are looked up via the pattern
    # table below — this profile value is the default for the FIRST
    # entry (kimi-k2-0905-preview).
    context_length=256_000,
    supports_thinking=True,  # K2 family streams reasoning_content.
    supports_tool_use_streaming=True,
    # Moonshot's compat shim DOES honour the cache_control marker
    # (B-320). The allowlist in openai.py picks this up via base_url
    # substring match — keeping the flag here lets a future
    # consistency check fire if either side drifts.
    supports_prompt_cache_marker=True,
    # Sprint 3 Iron Rule #3: Kimi-K2 (the chat-coding-tuned variant)
    # is strong on synthesis. Older moonshot-v1-32k is weaker but
    # still produces useful structured output — call it strong-tier
    # collectively.
    evolution_tier="strong",
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
    # qwen-plus baseline is 131K; qwen-max is 32K; qwen3-coder-plus is
    # 1M. Profile value matches the FIRST default (qwen-plus); the
    # pattern table below resolves the others when actually selected.
    context_length=131_072,
    # Qwen3 / QwQ stream reasoning_content; Qwen-Plus / Max / Turbo do
    # not — same trade-off as DeepSeek: enable the wiring, harmless on
    # non-reasoning models.
    supports_thinking=True,
    supports_tool_use_streaming=True,
    # DashScope's compat shim does NOT advertise cache_control support.
    # It does report cache hits via prompt_tokens_details.cached_tokens
    # (auto-caching), captured by _extract_cache_tokens already.
    supports_prompt_cache_marker=False,
    # Sprint 3 Iron Rule #3: Qwen-Max + qwen3-coder-plus are strong;
    # qwen-turbo is medium. Profile-level "medium" is a conservative
    # average — operators can flip up via the per-call override path
    # (deferred follow-up) when they know they're using the bigger
    # variant.
    evolution_tier="medium",
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
    # Gemini 2.5 Flash & Pro both ship with 1M context. 3-flash-preview
    # ships with 2M but XMclaw caps at 1M to stay within DOH-safe
    # estimator bounds.
    context_length=1_000_000,
    # Gemini 2.5 Flash / Pro emit thinking blocks via the native API;
    # the OpenAI-compat shim documents `reasoning` deltas in
    # streaming. Toggle on so the streaming wiring picks them up.
    supports_thinking=True,
    supports_tool_use_streaming=True,
    # Gemini's compat shim does NOT honour cache_control (Google has
    # its own context-caching API on the native side).
    supports_prompt_cache_marker=False,
    # Sprint 3 Iron Rule #3: Gemini 2.5 Pro is strong; Flash is
    # medium. Same compromise as Qwen — call profile-level "medium"
    # and let per-call override push to "strong" when known-Pro.
    evolution_tier="medium",
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


# ── Sprint 3 Iron Rule #3: model-id → evolution tier ──────────────
#
# Native providers (Anthropic / OpenAI) don't have ProviderProfile
# entries — they go through their own adapters. But the evolution
# loop still needs to ask "is this model strong enough to do
# self-extension / mutation / distillation?". This table classifies
# any model id by substring match.
#
# Honest disclosure: tiers are heuristic, sourced from Live-SWE-agent
# issue #7 ("GPT-5 / GPT-5-mini do NOT synthesise tools") + community
# reports + our own Sprint 4 benchmark (pending). When in doubt about
# a model, fall through to "unknown" and treat as "medium" downstream.
# The cost of mis-tiering is bounded — distill / mutator are advisory;
# nothing auto-promotes off their output (Iron Rule #1 + #2).

# Ordered list — first match wins. Patterns are lower-cased substrings.
# CRITICAL: more-specific patterns MUST come first. ``gpt-4o-mini``
# MUST appear before ``gpt-4o`` or the latter would shadow the former
# (substring overlap). Same for ``gpt-5-mini`` vs ``gpt-5``,
# ``o1-mini`` vs ``o1-pro``, etc.
_TIER_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    # ── WEAK first: more-specific mini / nano variants of bigger
    # flagships need to be classified before the broader pattern
    # would falsely capture them.
    ("gpt-5-mini", "weak"),
    ("gpt-5-nano", "weak"),
    ("gpt-3.5", "weak"),
    ("llama-3.1-8b", "weak"),
    ("llama-3.2-3b", "weak"),
    ("llama-3.2-1b", "weak"),
    ("qwen-turbo", "weak"),
    # ── MEDIUM second: same logic — ``gpt-4o-mini`` must beat ``gpt-4o``;
    # ``o1-mini`` must beat ``o1-pro``.
    ("gpt-4o-mini", "medium"),
    ("gpt-4-mini", "medium"),
    ("gpt-4.1", "medium"),
    ("o1-mini", "medium"),
    ("claude-haiku", "medium"),
    ("gemini-2.5-pro", "medium"),
    ("gemini-2.5-flash", "medium"),
    ("qwen-max", "medium"),
    ("qwen-plus", "medium"),
    ("qwen3-coder", "medium"),
    ("moonshot-v1-128k", "medium"),
    # ── STRONG last: the broad flagship matches catch any model id
    # whose mini / variant didn't classify it earlier.
    ("claude-opus-4", "strong"),
    ("claude-sonnet-4", "strong"),
    ("claude-3-5-sonnet", "strong"),
    ("claude-3-7", "strong"),
    ("gpt-4o", "strong"),
    ("gpt-4-turbo", "strong"),
    ("o1-preview", "strong"),
    ("o1-pro", "strong"),
    ("deepseek-v3", "strong"),
    ("deepseek-r1", "strong"),
    ("kimi-k2", "strong"),
    # Note: gpt-5 (the actual flagship, not the "mini" or "nano" SKUs)
    # is intentionally NOT listed. As of 2026 its evolution-task
    # behaviour varies wildly by tooling — some users report it
    # works fine, others see Live-SWE-style silent degradation. We
    # default it to "unknown" → downstream "medium" handling, which
    # is the conservative balance.
)


# ── Wave 26 fix-4: model id → context-window size ────────────────
#
# Wired into the compressor so the threshold gate (0.85 × ctx_len)
# fires at the right point for each model. The 200K default that
# used to be hardcoded meant:
#   - Kimi-K2 (256K)         compressed at 100K  → 39% utilization
#   - GPT-4o (128K)          compressed at 100K  → 78% utilization (close)
#   - Gemini-2.5-Pro (1M)    compressed at 100K  → 10% utilization
#   - DeepSeek-Chat (64K)    compressed at 100K  → already overflowed
#
# Now: per-model lookup → 85% threshold → right amount of compression
# for the right model. Most-specific patterns first (substring match
# inside a lower-cased model id). Fallback: 200K.

# Exact-match table — model ids we know the precise context length of.
_MODEL_CONTEXT_LENGTHS: Final[dict[str, int]] = {
    # Anthropic — 1M tier (Sonnet 4.6 / Opus 4.7 with 1m context flag).
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-7[1m]": 1_000_000,
    "claude-sonnet-4-6[1m]": 1_000_000,
    # Anthropic — 200K default tier.
    "claude-opus-4-5": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-7-sonnet-20250219": 200_000,
    # Kimi family — sizes encoded in the name except for K2.
    "kimi-k2-0905-preview": 256_000,
    "kimi-k2.6": 256_000,
    "kimi-k1.5": 200_000,
    "moonshot-v1-8k": 8_000,
    "moonshot-v1-32k": 32_000,
    "moonshot-v1-128k": 128_000,
    # Qwen family — Alibaba official caps.
    "qwen-plus": 131_072,
    "qwen-max": 32_768,
    "qwen-turbo": 1_000_000,
    "qwen3-coder-plus": 1_000_000,
    # DeepSeek — 64K as of Jan 2026.
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
    "deepseek-v3": 128_000,
    # Gemini family — all 2.5+ tier ships with 1M+.
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    "gemini-3-flash-preview": 1_000_000,
    # OpenAI flagships.
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_000_000,
    "gpt-4-turbo": 128_000,
    "o1-preview": 128_000,
    "o1-pro": 200_000,
    "o1-mini": 128_000,
    "gpt-5": 256_000,
    "gpt-5-mini": 256_000,
    "gpt-5-nano": 256_000,
}

# Substring patterns — first-match wins. Order matters: more-specific
# patterns MUST come before more-general ones (``kimi-k2`` before
# ``kimi`` etc.). Lower-cased.
_CONTEXT_LENGTH_PATTERNS: Final[tuple[tuple[str, int], ...]] = (
    # Anthropic — explicit 1m suffix (e.g. claude-opus-4-7[1m]).
    ("[1m]", 1_000_000),
    ("-1m", 1_000_000),
    # Anthropic family fallbacks.
    ("claude-opus-4-7", 1_000_000),
    ("claude-sonnet-4-6", 1_000_000),
    ("claude-haiku-4", 200_000),
    ("claude-opus-4", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-3-5", 200_000),
    ("claude-3-7", 200_000),
    # Kimi family.
    ("kimi-k2", 256_000),
    ("kimi-k1", 200_000),
    ("moonshot-v1-128k", 128_000),
    ("moonshot-v1-32k", 32_000),
    ("moonshot-v1-8k", 8_000),
    # Qwen family.
    ("qwen-turbo", 1_000_000),
    ("qwen3-coder", 1_000_000),
    ("qwen3", 131_072),
    ("qwen-plus", 131_072),
    ("qwen-max", 32_768),
    # DeepSeek family.
    ("deepseek-v3", 128_000),
    ("deepseek-chat", 64_000),
    ("deepseek-reasoner", 64_000),
    ("deepseek-r1", 64_000),
    # Gemini family.
    ("gemini-3", 1_000_000),
    ("gemini-2.5-pro", 1_000_000),
    ("gemini-2.5-flash", 1_000_000),
    ("gemini-2", 1_000_000),
    # OpenAI family.
    ("gpt-5", 256_000),
    ("gpt-4.1", 1_000_000),
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-4", 128_000),
    ("gpt-3.5", 16_385),
    ("o1-pro", 200_000),
    ("o1-mini", 128_000),
    ("o1-preview", 128_000),
    ("o1", 200_000),
)

# Sentinel default for completely unknown models. 200K matches the
# Anthropic Sonnet baseline and is conservative enough that even a
# small-window model (32K) won't compress before its real ceiling
# (because the threshold gate also checks the provider's actual
# prompt_tokens from a successful response — see compressor.py:198
# ``last_prompt_tokens``).
DEFAULT_CONTEXT_LENGTH: Final[int] = 200_000


def get_model_context_length(
    model: str | None, *, provider_id: str | None = None,
) -> int:
    """Return the context-window size in tokens for a given model id.

    Lookup order:
      1. Exact match in :data:`_MODEL_CONTEXT_LENGTHS`.
      2. Substring pattern in :data:`_CONTEXT_LENGTH_PATTERNS`
         (first-match wins; ordered most-specific-first).
      3. If ``provider_id`` is given, fall back to the profile's
         declared ``context_length`` so a deployment that pins
         ``llm.openai.api_base = some-anthropic-proxy`` still gets
         the Anthropic profile's window.
      4. :data:`DEFAULT_CONTEXT_LENGTH` (200K).

    Args:
        model: model id (lowercased internally for matching).
        provider_id: optional provider id from config — used as
            tiebreaker when the model id is ambiguous.
    """
    if not isinstance(model, str) or not model:
        if provider_id:
            profile = get_profile(provider_id)
            if profile:
                return profile.context_length
        return DEFAULT_CONTEXT_LENGTH
    lowered = model.lower()
    exact = _MODEL_CONTEXT_LENGTHS.get(lowered)
    if exact is not None:
        return exact
    for pat, ctx in _CONTEXT_LENGTH_PATTERNS:
        if pat in lowered:
            return ctx
    if provider_id:
        profile = get_profile(provider_id)
        if profile:
            return profile.context_length
    return DEFAULT_CONTEXT_LENGTH


def classify_model_tier(model: str | None) -> str:
    """Sprint 3 Iron Rule #3 — return an evolution tier for a model id.

    Returns one of ``"strong"`` / ``"medium"`` / ``"weak"`` /
    ``"unknown"``. Lookup is case-insensitive substring match against
    :data:`_TIER_PATTERNS`. ``None`` / empty / non-string → ``"unknown"``.

    Used by ReasoningBank distiller (skip for "weak") and
    ReflectiveMutator (skip for "weak"). The agent_loop's
    step-reflection prompt is gated on this too: "strong" gets the
    self-extension prompt, "medium" gets a constrained variant,
    "weak" gets template-fill mode (no synthesis).
    """
    if not isinstance(model, str) or not model:
        return "unknown"
    lowered = model.lower()
    for pat, tier in _TIER_PATTERNS:
        if pat in lowered:
            return tier
    return "unknown"


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
    "DEFAULT_CONTEXT_LENGTH",
    "GEMINI",
    "KIMI",
    "PROFILES",
    "QWEN",
    "ProviderProfile",
    "classify_model_tier",
    "detect_profile_from_base_url",
    "get_model_context_length",
    "get_profile",
    "list_profiles",
]
