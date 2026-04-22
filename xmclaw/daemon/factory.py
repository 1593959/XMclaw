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
import os
from pathlib import Path
from typing import Any, Mapping

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


ENV_PREFIX = "XMC__"
_ENV_PATH_SEP = "__"


def _coerce_scalar(raw: str) -> Any:
    """Best-effort type inference for an ENV string.

    Order: JSON (catches ints/floats/bools/null/arrays/objects), then
    fall back to the literal string. Keeps ``"true"``, ``"false"``,
    ``"null"``, ``"42"``, ``"3.14"``, and JSON literals typed, while
    leaving bare tokens like ``"sk-xxx"`` untouched.
    """
    s = raw.strip()
    if s == "":
        return raw
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return raw


def _apply_env_overrides(
    cfg: dict[str, Any],
    env: Mapping[str, str] | None = None,
    *,
    prefix: str = ENV_PREFIX,
) -> dict[str, Any]:
    """Merge ``XMC__<dotted_path>`` env vars into a config dict.

    Rules:
      * Key must start with ``prefix`` (default ``XMC__``).
      * Remainder is split on ``__`` into a path; each segment is
        lowercased and used as a nested dict key.
      * Scalar values go through :func:`_coerce_scalar` so ``"42"`` /
        ``"true"`` / JSON literals become proper typed values; plain
        strings stay as-is.
      * ENV wins over any existing value at the same path.
      * If a parent path is currently a non-dict scalar, it is
        overwritten by a new dict — ENV intent always wins, matching
        Pydantic Settings semantics.
      * Empty path segments are ignored (e.g. trailing ``__``), so
        ``XMC__llm____api_key`` is treated as ``XMC__llm__api_key``.

    Mutates and returns ``cfg``.
    """
    source = os.environ if env is None else env
    for raw_key, raw_val in source.items():
        if not raw_key.startswith(prefix):
            continue
        remainder = raw_key[len(prefix):]
        if not remainder:
            continue
        segments = [s.lower() for s in remainder.split(_ENV_PATH_SEP) if s]
        if not segments:
            continue
        cursor: dict[str, Any] = cfg
        for seg in segments[:-1]:
            nxt = cursor.get(seg)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[seg] = nxt
            cursor = nxt
        cursor[segments[-1]] = _coerce_scalar(raw_val)
    return cfg


def load_config(
    path: Path | str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read a JSON config from disk, then overlay ``XMC__*`` env vars.

    Precedence (highest last): file → ENV. Kept as a standalone
    function so tests that want to exercise the factory with a dict
    can skip the filesystem round-trip; pass ``env={}`` to disable
    overrides in tests.
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
    return _apply_env_overrides(data, env=env)


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
    """Return a ``ToolProvider`` built from ``cfg['tools']``.

    Posture (v2 update): permissions default to MAXIMUM, not minimum.
    A local AI assistant the user deliberately installed is not a hostile
    sandbox by default -- it needs to read the Desktop, run shell
    commands, and hit the network to be useful. Earlier phases defaulted
    to tool-less-with-required-allowlist; that produced the
    "list my Desktop -> permission denied" experience that the user
    reasonably called unusable.

    Config shape:

    ::

        {
          "tools": {
            # Optional sandbox -- if present AND non-empty, filesystem
            # tools refuse paths outside these dirs. If omitted (or []),
            # filesystem tools have full user-level access.
            "allowed_dirs": ["/path/a", "/path/b"],
            # Optional kill-switches; default True for each.
            "enable_bash": true,
            "enable_web":  true
          }
        }

    No ``tools`` section -> full-access BuiltinTools with every tool
    family enabled. Users who want a sandbox opt in by adding
    ``allowed_dirs`` or flipping the enable_ flags.
    """
    tools_section = cfg.get("tools")
    if tools_section is None:
        # Default posture: full access, all tool families on.
        return BuiltinTools()
    if not isinstance(tools_section, dict):
        raise ConfigError(
            f"'tools' must be an object, got {type(tools_section).__name__}"
        )

    allowed_dirs = tools_section.get("allowed_dirs")
    if allowed_dirs is not None:
        if not isinstance(allowed_dirs, list):
            raise ConfigError(
                f"'tools.allowed_dirs' must be a list, got "
                f"{type(allowed_dirs).__name__}"
            )
        for entry in allowed_dirs:
            if not isinstance(entry, str):
                raise ConfigError(
                    f"'tools.allowed_dirs' entries must be strings, got "
                    f"{type(entry).__name__}: {entry!r}"
                )
        # Empty list -> no sandbox (same as omitting the key). Users who
        # want "deny everything" can set enable_bash/enable_web to false
        # explicitly; an empty allowlist as "block everything" is too
        # easy to trip over by accident.
        if len(allowed_dirs) == 0:
            allowed_dirs = None

    enable_bash = tools_section.get("enable_bash", True)
    enable_web = tools_section.get("enable_web", True)
    enable_browser = tools_section.get("enable_browser", False)
    enable_lsp = tools_section.get("enable_lsp", False)

    builtins = BuiltinTools(
        allowed_dirs=allowed_dirs,
        enable_bash=bool(enable_bash),
        enable_web=bool(enable_web),
    )
    children: list[ToolProvider] = [builtins]

    if enable_browser:
        try:
            from xmclaw.providers.tool.browser import BrowserTools
            bcfg = tools_section.get("browser", {}) or {}
            children.append(BrowserTools(
                allowed_hosts=bcfg.get("allowed_hosts"),
                headless=bool(bcfg.get("headless", True)),
                timeout_ms=int(bcfg.get("timeout_ms", 15_000)),
            ))
        except ImportError:
            # playwright optional-dep not installed -- log-skippable,
            # don't crash the daemon over it. The admin gets a heads-up
            # via the factory's build summary in serve().
            pass

    if enable_lsp:
        try:
            from xmclaw.providers.tool.lsp import LSPTools
            lcfg = tools_section.get("lsp", {}) or {}
            children.append(LSPTools(
                root=lcfg.get("root") or ".",
                startup_timeout_s=float(lcfg.get("startup_timeout_s", 10.0)),
            ))
        except ImportError:
            pass

    if len(children) == 1:
        return builtins  # no extras wired -- skip the composite wrapper

    from xmclaw.providers.tool.composite import CompositeToolProvider
    return CompositeToolProvider(*children)


def build_agent_from_config(
    cfg: dict[str, Any],
    bus: InProcessEventBus,
    *,
    max_hops: int = 20,
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
