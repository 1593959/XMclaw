"""Config → runtime-object factories for the v2 daemon.

Phase 4.2 deliverable. Reads the same ``daemon/config.json`` shape as
v1 (users maintain one config), picks the first LLM provider with a
configured api_key, and builds a ready-to-use ``AgentLoop``.

Scope of this module:
  * ``build_llm_from_config(cfg)`` — pick provider + instantiate it
  * ``build_agent_from_config(cfg, bus)`` — assemble an AgentLoop
  * ``load_config(path)`` — thin wrapper over json.load (kept out of
    the factory so tests can pass in-memory dicts directly)

Non-goals (deferred):
  * ToolProvider wiring — Phase 4.3 adds a config section for tools
    with a file-system allowlist. For now the factory builds a tool-
    less AgentLoop.
  * Device-bound auth — Phase 4.4.
  * Scheduler + evolution controller on top of the agent — Phase 4.3.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.llm.base import LLMProvider
from xmclaw.providers.llm.openai import OpenAILLM


# Recognised provider kinds in the config. Each maps to a constructor.
_PROVIDER_ORDER: tuple[str, ...] = ("anthropic", "openai")


class ConfigError(ValueError):
    """Raised when the config is structurally invalid or incomplete."""


def load_config(path: Path | str) -> dict[str, Any]:
    """Read a JSON config from disk. Raises ConfigError if unreadable.

    Kept as a standalone function so tests that want to exercise the
    factory with a dict can skip the filesystem round-trip.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(
            f"config root must be an object, got {type(data).__name__}"
        )
    return data


def build_llm_from_config(cfg: dict[str, Any]) -> LLMProvider | None:
    """Return an LLMProvider constructed from ``cfg['llm'][<provider>]``.

    Selects the first provider in ``_PROVIDER_ORDER`` that has a
    non-empty ``api_key``. Returns ``None`` if no provider is
    configured — callers should treat this as "run the daemon in echo
    mode" rather than an error, since that's a valid posture for
    local-only tool-loop work.

    Raises ``ConfigError`` only for STRUCTURAL problems in the
    ``llm`` section (e.g. it exists but isn't a dict).
    """
    llm_section = cfg.get("llm")
    if llm_section is None:
        return None
    if not isinstance(llm_section, dict):
        raise ConfigError(
            f"'llm' must be an object, got {type(llm_section).__name__}"
        )

    for provider_name in _PROVIDER_ORDER:
        pcfg = llm_section.get(provider_name)
        if not isinstance(pcfg, dict):
            continue
        api_key = pcfg.get("api_key")
        if not api_key or not isinstance(api_key, str):
            continue

        model = (
            pcfg.get("default_model")
            or pcfg.get("model")
            or _default_model_for(provider_name)
        )
        base_url = pcfg.get("base_url")
        if provider_name == "anthropic":
            return AnthropicLLM(
                api_key=api_key, model=model, base_url=base_url or None,
            )
        if provider_name == "openai":
            return OpenAILLM(
                api_key=api_key, model=model, base_url=base_url or None,
            )

    return None


def _default_model_for(provider_name: str) -> str:
    """Last-ditch default — used only when the config omits model."""
    return {
        "anthropic": "claude-haiku-4-5-20251001",
        "openai": "gpt-4o-mini",
    }.get(provider_name, "")


def build_agent_from_config(
    cfg: dict[str, Any],
    bus: InProcessEventBus,
    *,
    max_hops: int = 5,
) -> AgentLoop | None:
    """Assemble an AgentLoop from config. Returns None if no LLM is set.

    The returned agent has no ToolProvider (Phase 4.3 adds that).
    """
    llm = build_llm_from_config(cfg)
    if llm is None:
        return None
    return AgentLoop(
        llm=llm, bus=bus, tools=None,
        max_hops=max_hops,
        agent_id=cfg.get("agent_id", "agent"),
    )
