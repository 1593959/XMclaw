"""Config → runtime-object factories for the v2 daemon.

Phase 4.2 deliverable. Reads the same ``daemon/config.json`` shape as
v1 (users maintain one config), picks the first LLM provider with a
configured api_key, and builds a ready-to-use ``AgentLoop``.

Scope of this module:
  * ``build_llm_from_config(cfg)`` — pick provider + instantiate it
  * ``build_tools_from_config(cfg)`` — assemble ToolProvider composite
  * ``build_memory_from_config(cfg, bus)`` — optional SqliteVecMemory
    + retention caps (Epic #5)
  * ``build_skill_runtime_from_config(cfg)`` — pick Local / Process
    SkillRuntime (Epic #3 entry point; no caller yet)
  * ``build_agent_from_config(cfg, bus)`` — assemble an AgentLoop
  * ``load_config(path)`` — thin wrapper over json.load (kept out of
    the factory so tests can pass in-memory dicts directly)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.llm.base import LLMProvider
from xmclaw.providers.llm.openai import OpenAILLM
from xmclaw.providers.memory.sqlite_vec import SqliteVecMemory
from xmclaw.providers.runtime import (
    LocalSkillRuntime,
    ProcessSkillRuntime,
    SkillRuntime,
)
from xmclaw.providers.tool.base import ToolProvider
from xmclaw.providers.tool.builtin import BuiltinTools
from xmclaw.security.prompt_scanner import PolicyMode
from xmclaw.utils.paths import default_memory_db_path


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


# ── Epic #16 Phase 2: ${secret:name} placeholder resolution ──────────────
#
# After the env overlay lands, walk the config dict recursively and replace
# any string value that is EXACTLY the shape ``${secret:NAME}`` with the
# value returned by :func:`xmclaw.utils.secrets.get_secret`. This generalises
# the per-field fallback that ``build_llm_from_config`` does (empty literal
# → secrets lookup) into a config-wide mechanism — useful for any leaf that
# ends up holding a credential (tool API keys, channel tokens, memory
# backend passwords …).
#
# Rules:
#   * Matching is **whole-string only** (anchored): ``"${secret:x}"`` matches,
#     ``"prefix-${secret:x}-suffix"`` does NOT — partial substitution
#     invites silent escaping bugs that are exactly the wrong place to have
#     them. If someone needs concatenation they can pre-assemble in secrets.
#   * Name charset: ``[A-Za-z0-9_.-]+`` — same shape ``get_secret`` already
#     accepts (it lowercases & dots; the resolver just hands the raw name
#     through). Empty name → ``ConfigError`` ("malformed").
#   * Resolution failure (secret returns ``None``) → ``ConfigError``. We do
#     NOT silently drop to ``None`` / empty string; if you typed
#     ``${secret:X}`` you meant "this MUST be resolved at startup", and a
#     silent fall-through would e.g. turn an LLM API key into a stealth
#     echo-mode. Loud failure > stealthy degradation.
#   * Non-string values are untouched (numbers, bools, None, nested dicts
#     and lists are all traversed; only strings are candidates).
#   * Lists traverse element-wise; nested dicts recurse.

_SECRET_PLACEHOLDER_RE = re.compile(r"^\$\{secret:([A-Za-z0-9_.\-]+)\}$")
# Catches malformed placeholders like ``${secret:}`` (empty) or
# ``${secret: foo }`` (whitespace padding) so users get a clear error
# rather than a silent miss against the wrong key.
_SECRET_SHAPE_RE = re.compile(r"^\$\{secret:.*\}$")


def _resolve_secret_placeholders(
    value: Any,
    *,
    _resolver=None,
    _path: str = "$",
) -> Any:
    """Recursively walk ``value`` replacing ``${secret:NAME}`` strings.

    ``_resolver`` lets tests inject a fake ``get_secret`` without touching
    the real secrets layer. Default: :func:`xmclaw.utils.secrets.get_secret`
    (imported lazily to avoid an import cycle during daemon bootstrap).
    """
    if _resolver is None:
        from xmclaw.utils.secrets import get_secret  # local to dodge cycles

        _resolver = get_secret

    if isinstance(value, str):
        m = _SECRET_PLACEHOLDER_RE.match(value)
        if m:
            name = m.group(1)
            resolved = _resolver(name)
            if resolved is None:
                raise ConfigError(
                    f"unresolved secret at {_path}: ${{secret:{name}}} "
                    f"(run `xmclaw config set-secret {name}` or set "
                    f"env XMC_SECRET_{name.upper().replace('.', '_').replace('-', '_')})"
                )
            return resolved
        # Catch malformed placeholders that *look* like the syntax but
        # didn't match the strict pattern — otherwise we'd silently let
        # typos through as literals.
        if _SECRET_SHAPE_RE.match(value):
            raise ConfigError(
                f"malformed secret placeholder at {_path}: {value!r} "
                "(expected ${secret:NAME} with NAME matching [A-Za-z0-9_.-]+)"
            )
        return value
    if isinstance(value, dict):
        return {
            k: _resolve_secret_placeholders(v, _resolver=_resolver, _path=f"{_path}.{k}")
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _resolve_secret_placeholders(v, _resolver=_resolver, _path=f"{_path}[{i}]")
            for i, v in enumerate(value)
        ]
    return value


def load_config(
    path: Path | str,
    *,
    env: Mapping[str, str] | None = None,
    resolve_secrets: bool = True,
) -> dict[str, Any]:
    """Read a JSON config from disk, then overlay ``XMC__*`` env vars
    and resolve any ``${secret:NAME}`` placeholders.

    Precedence (highest last): file → ENV → secret resolution. Kept as
    a standalone function so tests that want to exercise the factory
    with a dict can skip the filesystem round-trip; pass ``env={}`` to
    disable env overrides and ``resolve_secrets=False`` to keep
    placeholders intact (useful when round-tripping config for export).

    ``resolve_secrets=True`` (default) walks the merged dict recursively
    and replaces each whole-string ``${secret:name}`` with the value
    returned by :func:`xmclaw.utils.secrets.get_secret`. Unresolvable
    references raise :class:`ConfigError` — see
    :func:`_resolve_secret_placeholders` for the exact rules.
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
    merged = _apply_env_overrides(data, env=env)
    if resolve_secrets:
        merged = _resolve_secret_placeholders(merged)
    return merged


def build_llm_from_config(cfg: dict[str, Any]) -> LLMProvider | None:
    """Return an LLMProvider constructed from ``cfg['llm'][<provider>]``.

    Selects the first provider in ``_PROVIDER_ORDER`` that has a
    resolvable ``api_key``. ``None`` if no provider is configured —
    callers should treat that as "run the daemon in echo mode" rather
    than an error, since that's a valid posture for local-only
    tool-loop work.

    Key resolution order (introduced alongside Epic #16 Phase 1):

    1. The literal string in ``cfg['llm'][<name>]['api_key']``.
    2. When that's empty / missing / whitespace-only, fall back to
       :func:`xmclaw.utils.secrets.get_secret` with the dotted name
       ``llm.<provider>.api_key``.

    Concretely: leaving ``api_key: ""`` in config.json and running
    ``xmclaw config set-secret llm.anthropic.api_key`` is now a
    first-class way to keep cleartext keys out of the JSON. Users
    who prefer the old path (inline string in config.json) are
    untouched — the literal wins when it's non-empty.

    Raises ``ConfigError`` only for STRUCTURAL problems in the
    ``llm`` section (e.g. it exists but isn't a dict).
    """
    from xmclaw.utils.secrets import get_secret

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
        raw_key = pcfg.get("api_key")
        api_key = raw_key if isinstance(raw_key, str) else None

        # Epic #16 fallback: empty / missing cfg → try the secrets layer.
        # get_secret() already handles env > file > keyring and treats
        # whitespace-only as miss, so we just consult it and move on.
        if not api_key or not api_key.strip():
            api_key = get_secret(f"llm.{provider_name}.api_key")
        if not api_key or not api_key.strip():
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


def build_memory_from_config(
    cfg: dict[str, Any],
    bus: InProcessEventBus | None = None,
) -> SqliteVecMemory | None:
    """Return a ``SqliteVecMemory`` built from ``cfg['memory']``.

    Returns ``None`` when ``cfg['memory'].enabled`` is falsy so the
    daemon can omit memory entirely (unit-test mode, pure-chat
    scenarios). Default when the ``memory`` section is missing is
    enabled-on.

    Config shape (all fields optional):

    ::

        {
          "memory": {
            "enabled": true,                 # default: true
            "db_path": "<path>",             # default: ~/.xmclaw/v2/memory.db
            "embedding_dim": null,           # int or null
            "ttl": {                         # seconds per layer; null = never
              "short": 3600,
              "working": 86400,
              "long": null
            },
            "pinned_tags": ["identity"],     # never-evict tag allowlist
            "retention": {                   # Epic #5 periodic sweep caps
              "max_items": {"short": 2000, "working": 20000, "long": null},
              "max_bytes": {"short": null, "working": null, "long": null},
              "sweep_interval_s": 3600
            }
          }
        }

    The ``retention`` sub-section is consumed by
    ``xmclaw.daemon.memory_sweep.MemorySweepTask``; this factory only
    mints the memory store itself. Pass ``bus`` so evictions emit
    ``MEMORY_EVICTED`` events.
    """
    mem_section = cfg.get("memory")
    if mem_section is None:
        db_path = default_memory_db_path()
        return SqliteVecMemory(db_path, bus=bus)
    if not isinstance(mem_section, dict):
        raise ConfigError(
            f"'memory' must be an object, got {type(mem_section).__name__}"
        )
    if mem_section.get("enabled") is False:
        return None

    db_path_raw = mem_section.get("db_path")
    db_path: Path | str
    if isinstance(db_path_raw, str) and db_path_raw:
        db_path = db_path_raw
    else:
        db_path = default_memory_db_path()

    embedding_dim = mem_section.get("embedding_dim")
    if embedding_dim is not None and not isinstance(embedding_dim, int):
        raise ConfigError(
            f"'memory.embedding_dim' must be int or null, got "
            f"{type(embedding_dim).__name__}"
        )

    ttl = mem_section.get("ttl")
    if ttl is not None and not isinstance(ttl, dict):
        raise ConfigError(
            f"'memory.ttl' must be an object, got {type(ttl).__name__}"
        )

    pinned_tags = mem_section.get("pinned_tags")
    if pinned_tags is not None:
        if not isinstance(pinned_tags, list) or not all(
            isinstance(t, str) for t in pinned_tags
        ):
            raise ConfigError("'memory.pinned_tags' must be a list of strings")

    return SqliteVecMemory(
        db_path,
        embedding_dim=embedding_dim,
        ttl=ttl,
        pinned_tags=tuple(pinned_tags) if pinned_tags else None,
        bus=bus,
    )


_RUNTIME_BACKENDS: dict[str, type[SkillRuntime]] = {
    "local": LocalSkillRuntime,
    "process": ProcessSkillRuntime,
}


def build_skill_runtime_from_config(cfg: dict[str, Any]) -> SkillRuntime:
    """Return a ``SkillRuntime`` picked from ``cfg['runtime']``.

    Config shape::

        {
          "runtime": {
            "backend": "local" | "process"
          }
        }

    Default is ``"local"`` — the in-process runtime is fine for dev and
    for conformance tests that assume a fast startup. Production
    deployments should set ``"process"`` for real subprocess isolation
    (see ``xmclaw.providers.runtime.process`` for the honest scope of
    what that gives you vs a true container sandbox).

    Raises ``ConfigError`` on an unknown backend or malformed section.
    There is no ``enabled: false`` switch — a daemon without any skill
    runtime has nowhere to execute skills.
    """
    rt_section = cfg.get("runtime")
    if rt_section is None:
        return LocalSkillRuntime()
    if not isinstance(rt_section, dict):
        raise ConfigError(
            f"'runtime' must be an object, got {type(rt_section).__name__}"
        )
    backend = rt_section.get("backend", "local")
    if not isinstance(backend, str):
        raise ConfigError(
            f"'runtime.backend' must be a string, got "
            f"{type(backend).__name__}"
        )
    cls = _RUNTIME_BACKENDS.get(backend)
    if cls is None:
        known = ", ".join(sorted(_RUNTIME_BACKENDS))
        raise ConfigError(
            f"'runtime.backend' must be one of {{{known}}}, got {backend!r}"
        )
    return cls()


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

    Epic #14: ``security.prompt_injection`` (string: ``detect_only`` /
    ``redact`` / ``block``) is read here and handed to the loop. Missing
    or unrecognised values fall back to ``detect_only``.
    """
    llm = build_llm_from_config(cfg)
    if llm is None:
        return None
    tools = build_tools_from_config(cfg)
    security = cfg.get("security")
    policy_raw = None
    if isinstance(security, Mapping):
        policy_raw = security.get("prompt_injection")
        if not isinstance(policy_raw, str):
            policy_raw = None
    policy = PolicyMode.parse(policy_raw, default=PolicyMode.DETECT_ONLY)
    return AgentLoop(
        llm=llm, bus=bus, tools=tools,
        max_hops=max_hops,
        agent_id=cfg.get("agent_id", "agent"),
        prompt_injection_policy=policy,
    )
