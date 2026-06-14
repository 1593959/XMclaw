"""LLMRegistry — keep N constructed LLMProviders addressable by id.

Multi-model support (Phase: per-session picker). The user may deploy
several LLM endpoints (Anthropic Sonnet, OpenAI 4o-mini, a local
DeepSeek over an OpenAI-compatible base URL, …) and pick which one to
use per chat session. The registry holds those constructed providers
keyed by ``profile_id`` so the WebSocket handler can route turns
without re-instantiating SDK clients on every message.

Built once at daemon startup by
:func:`xmclaw.daemon.factory.build_llm_registry_from_config` and
attached to ``app.state.llm_registry``. AgentLoop reads it via
``run_turn(..., llm_profile_id=...)``.

Read-only: profiles added via the HTTP CRUD route are persisted to
``daemon/config.json`` and require a daemon restart to surface — the
SDK clients hold connection state we don't want to hot-swap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from xmclaw.providers.llm.base import LLMProvider

# Vocabulary for `LLMProfile.capabilities`. Open set: callers may pass
# strings outside this list (e.g. an experimental `stt_realtime`); the
# registry never gates on membership. Listed here so type checkers /
# editors can autocomplete the common cases.
#
#   text       \u2014 chat / completion (assumed for every LLMProvider)
#   vision     \u2014 image input (PNG/JPG -> understanding)
#   audio_in   \u2014 speech / audio input (STT)
#   audio_out  \u2014 speech / audio output (TTS)
#   image_gen  \u2014 generates images (text -> image)
#   video_gen  \u2014 generates video (text -> video)
#   embedding  \u2014 produces vectors
#   reasoning  \u2014 long-chain reasoning (o1/Opus/R1 class)
#   tools      \u2014 native tool/function calling
#   multimodal \u2014 single model that natively mixes >=2 input modalities
KNOWN_CAPABILITIES: tuple[str, ...] = (
    "text",
    "vision",
    "audio_in",
    "audio_out",
    "image_gen",
    "video_gen",
    "embedding",
    "reasoning",
    "tools",
    "multimodal",
)


@dataclass(frozen=True)
class LLMProfile:
    """One named LLM endpoint the user has configured.

    ``llm`` is the constructed provider (Anthropic / OpenAI / …).
    ``label`` is the human-friendly name shown in the chat header
    dropdown — falls back to ``id`` when the user didn't supply one.

    ``tier`` (Sprint 0 multi-model routing): semantic capability tier.
    The ``ModelTierRouter`` reads message complexity / vision needs /
    tool needs and picks one of:

      * ``"fast"``     — single-shot chitchat / commands (Qwen 7B,
                        Haiku, GPT-4o-mini). Latency target < 2s.
      * ``"balanced"`` — default for most turns (Sonnet, GPT-4o,
                        Kimi K2.6). Latency 5-15s, quality good.
      * ``"strong"``   — long-chain reasoning, complex tool use
                        (Opus 4.7, GPT-4.1). Quality > speed.
      * ``"vision"``   — vision-grounded GUI work (Sonnet 4.6,
                        GPT-4o, UI-TARS). Specialised.

    Unconfigured profiles default to ``"balanced"``.
    """

    id: str
    label: str
    provider_name: str   # "anthropic" / "openai" / future kinds
    model: str
    llm: LLMProvider
    tier: str = "balanced"
    # Modality / capability set used by routing helpers (see
    # `KNOWN_CAPABILITIES` above). Always include "text" for chat
    # models. Concrete factory call sites populate this from explicit
    # config first, falling back to `_infer_capabilities_from_model`.
    capabilities: frozenset[str] = field(default_factory=frozenset)
    # Optional human-friendly category override shown in the picker
    # ("chat" / "vision" / "tts" / "stt" / "image" / "video" /
    # "embedding"). When empty, the UI derives it from capabilities.
    category: str = ""


@dataclass
class LLMRegistry:
    """Map of ``profile_id`` → constructed LLMProvider.

    ``default_id`` names the profile picked when a session doesn't
    request one (i.e. the first profile that came online or the legacy
    single-LLM block from config). May be ``None`` when no profiles
    are configured at all — a fresh install before the user wires a
    key. AgentLoop tolerates that and runs in echo mode.
    """

    profiles: dict[str, LLMProfile] = field(default_factory=dict)
    default_id: str | None = None

    def __post_init__(self) -> None:
        if self.default_id is not None and self.default_id not in self.profiles:
            raise ValueError(
                f"default_id={self.default_id!r} is not in profiles "
                f"(have {sorted(self.profiles)})"
            )

    def get(self, profile_id: str) -> LLMProfile | None:
        """Lookup by id. Returns None on miss — caller decides whether
        to fall back to default or surface an error."""
        return self.profiles.get(profile_id)

    def default(self) -> LLMProfile | None:
        """The profile used when a session doesn't pick one."""
        if self.default_id is None:
            return None
        return self.profiles.get(self.default_id)

    def ids(self) -> list[str]:
        """Stable order: insertion-ordered (Python dict guarantee)."""
        return list(self.profiles.keys())

    def __iter__(self) -> Iterator[LLMProfile]:
        return iter(self.profiles.values())

    def __len__(self) -> int:
        return len(self.profiles)

    def __contains__(self, profile_id: object) -> bool:
        return profile_id in self.profiles

    def add_profile(self, profile: LLMProfile) -> None:
        """Hot-add a profile to the registry (no restart needed).

        Called by the model-discovery Apply endpoint to inject
        discovered models into the in-memory registry immediately.
        """
        self.profiles[profile.id] = profile

    # ── Sprint 0: tier-based picking ──────────────────────────────

    def by_tier(self, tier: str) -> list[LLMProfile]:
        """Return all profiles whose ``tier`` matches. Insertion order
        preserved so the FIRST hit is the preferred candidate."""
        return [p for p in self.profiles.values() if p.tier == tier]

    def pick_by_tier(
        self,
        tier: str,
        *,
        fallback_chain: tuple[str, ...] = (),
    ) -> LLMProfile | None:
        """Find a profile for ``tier``. If none registered for the
        requested tier, walk ``fallback_chain`` in order. Last resort
        is the registry default. Returns None only when the entire
        registry is empty.

        Typical fallback chains:
          * fast      → fallback ("balanced",)
          * vision    → fallback ("balanced", "strong")
          * strong    → fallback ("balanced",)
          * balanced  → fallback ("strong", "fast")

        ``ModelTierRouter`` constructs the chain based on what failure
        mode is least bad (e.g., for a vision-required turn, a non-
        vision balanced model is better than a 30s timeout).
        """
        hits = self.by_tier(tier)
        if hits:
            return hits[0]
        for fb in fallback_chain:
            hits = self.by_tier(fb)
            if hits:
                return hits[0]
        return self.default()


    # \u2500\u2500 Capability-based picking (Phase 11): per-task model choice. \u2500\u2500

    def by_capability(
        self,
        capability: str,
        *,
        require_all: tuple[str, ...] = (),
    ) -> list[LLMProfile]:
        """All profiles whose capability set includes `capability`.

        `require_all` lets the caller demand multiple capabilities at
        once \u2014 e.g. picking a model that does *both* vision and tools.
        Insertion order preserved so the first hit is the preferred one.
        """
        cap = (capability or "").strip().lower()
        if not cap:
            return []
        required = {cap, *(c.strip().lower() for c in require_all if c)}
        out: list[LLMProfile] = []
        for prof in self.profiles.values():
            caps = prof.capabilities or frozenset()
            if required.issubset(caps):
                out.append(prof)
        return out

    def pick_by_capability(
        self,
        capability: str,
        *,
        require_all: tuple[str, ...] = (),
        prefer_tier: tuple[str, ...] = (),
    ) -> LLMProfile | None:
        """Pick a single profile able to satisfy `capability`.

        Selection order:
          1. Exact capability match \u2014 highest priority of all.
          2. Among matches, prefer those whose `tier` appears earlier
             in `prefer_tier`. Useful when several models can do the
             job and we want the strong / vision tier first.
          3. Insertion order as last tiebreaker.
        Returns `None` when no profile lists the requested capability
        \u2014 callers fall back to a tier-based pick on their own (we don't
        silently route a video task to a chat-only model).
        """
        hits = self.by_capability(capability, require_all=require_all)
        if not hits:
            return None
        if not prefer_tier:
            return hits[0]
        order = {t: i for i, t in enumerate(prefer_tier)}
        hits.sort(key=lambda p: order.get(p.tier, len(prefer_tier)))
        return hits[0]