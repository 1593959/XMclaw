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
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.providers.tool.builtin import BuiltinTools


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


def build_tools_from_config(cfg: dict[str, Any]) -> ToolProvider | None:
    """Return a ``ToolProvider`` built from ``cfg['tools']`` — or ``None``
    when no tools section is present.

    Config shape (Phase 4.3):

    ::

        {
          "tools": {
            "allowed_dirs": ["/absolute/path", "/another/path"]
          }
        }

    Design choices:
      * No ``tools`` section → ``None``. Daemon starts in "LLM only,
        no tool calls will succeed" mode. This is deliberate — tools
        should be an explicit opt-in, not "magically enabled because
        the defaults leaked the filesystem".
      * ``tools`` section present but ``allowed_dirs`` missing or
        empty → ``ConfigError``. Refuses the ambiguous case where the
        admin enabled tools but didn't say where they can touch.
        BuiltinTools' own default (allowed_dirs=None) means "trust
        the caller"; via config we enforce the opposite posture
        (everything denied until explicitly allowed).
      * Every path in ``allowed_dirs`` is handed to BuiltinTools
        verbatim; BuiltinTools resolves + enforces them at runtime.

    Phase 4.x will extend this with MCP bridge config (a separate
    ``tools.mcp_servers`` section) and per-tool enable flags.
    """
    tools_section = cfg.get("tools")
    if tools_section is None:
        return None
    if not isinstance(tools_section, dict):
        raise ConfigError(
            f"'tools' must be an object, got {type(tools_section).__name__}"
        )
    allowed_dirs = tools_section.get("allowed_dirs")
    if allowed_dirs is None:
        raise ConfigError(
            "'tools.allowed_dirs' is required when the tools section is "
            "present -- set it to a non-empty list of paths the tools "
            "are allowed to read/write, or remove the tools section "
            "entirely to disable tools"
        )
    if not isinstance(allowed_dirs, list):
        raise ConfigError(
            f"'tools.allowed_dirs' must be a list, got "
            f"{type(allowed_dirs).__name__}"
        )
    if len(allowed_dirs) == 0:
        raise ConfigError(
            "'tools.allowed_dirs' must be non-empty -- an empty list "
            "would deny every path, which means tools are enabled but "
            "unusable. Remove the 'tools' section instead."
        )
    for entry in allowed_dirs:
        if not isinstance(entry, str):
            raise ConfigError(
                f"'tools.allowed_dirs' entries must be strings, got "
                f"{type(entry).__name__}: {entry!r}"
            )
    return BuiltinTools(allowed_dirs=allowed_dirs)


def build_agent_from_config(
    cfg: dict[str, Any],
    bus: InProcessEventBus,
    *,
    max_hops: int = 5,
) -> AgentLoop | None:
    """Assemble an AgentLoop from config. Returns None if no LLM is set.

    Wires both ``llm`` and ``tools`` sections. A config with an LLM
    but no tools section produces a tool-less AgentLoop (still usable
    for pure-chat scenarios). A config with tools but no LLM still
    returns None — tools without an agent have no caller.
    """
    llm = build_llm_from_config(cfg)
    if llm is None:
        return None
    tools = build_tools_from_config(cfg)
    return AgentLoop(
        llm=llm, bus=bus, tools=tools,
        max_hops=max_hops,
        agent_id=cfg.get("agent_id", "agent"),
    )
