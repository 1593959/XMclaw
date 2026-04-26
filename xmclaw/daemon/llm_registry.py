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
    """

    id: str
    label: str
    provider_name: str   # "anthropic" / "openai" / future kinds
    model: str
    llm: LLMProvider


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
