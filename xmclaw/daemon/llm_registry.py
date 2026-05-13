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
