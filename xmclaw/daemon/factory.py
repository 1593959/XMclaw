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
from xmclaw.daemon.llm_registry import LLMProfile, LLMRegistry
from xmclaw.daemon.session_store import SessionStore
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
from xmclaw.utils.paths import (
    default_memory_db_path,
    default_sessions_db_path,
    persona_dir,
)


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


def _instantiate_llm(
    provider_name: str, *, api_key: str, model: str, base_url: str | None,
) -> LLMProvider | None:
    """Construct one LLMProvider from already-resolved config values.

    Centralised here so both the legacy single-block path and the new
    profiles-array path build providers identically. Returns ``None``
    when ``provider_name`` is unknown — callers skip the entry rather
    than crashing the whole registry.
    """
    if provider_name == "anthropic":
        return AnthropicLLM(api_key=api_key, model=model, base_url=base_url or None)
    if provider_name == "openai":
        return OpenAILLM(api_key=api_key, model=model, base_url=base_url or None)
    return None


def build_llm_profiles_from_config(cfg: dict[str, Any]) -> list[LLMProfile]:
    """Build all named profiles from ``cfg['llm']['profiles']``.

    Schema for each entry::

        {
          "id": "haiku-fast",                # required, slug-ish
          "label": "Claude Haiku (fast)",    # optional, shown in UI
          "provider": "anthropic",           # required: anthropic|openai
          "model": "claude-haiku-4-5",       # required (or default_model)
          "api_key": "sk-...",               # required (or via secrets)
          "base_url": "https://..."          # optional
        }

    Profiles with missing/empty api_key after the secrets fallback are
    skipped silently — same posture as :func:`build_llm_from_config`,
    so a half-filled placeholder doesn't crash the daemon.

    Profiles with duplicate ``id`` keep the first occurrence; later
    duplicates are dropped (with no error — config is already on
    disk, the user can fix it and reload).
    """
    from xmclaw.utils.secrets import get_secret

    llm_section = cfg.get("llm")
    if not isinstance(llm_section, dict):
        return []
    raw_profiles = llm_section.get("profiles")
    if not isinstance(raw_profiles, list):
        return []

    out: list[LLMProfile] = []
    seen: set[str] = set()
    for entry in raw_profiles:
        if not isinstance(entry, dict):
            continue
        pid = str(entry.get("id") or "").strip()
        provider_name = str(entry.get("provider") or "").strip().lower()
        if not pid or pid in seen or provider_name not in _PROVIDER_ORDER:
            continue

        raw_key = entry.get("api_key")
        api_key = raw_key if isinstance(raw_key, str) else ""
        if not api_key.strip():
            api_key = get_secret(f"llm.profile.{pid}.api_key") or ""
        if not api_key.strip():
            continue

        model = str(
            entry.get("model")
            or entry.get("default_model")
            or _default_model_for(provider_name)
        ).strip()
        if not model:
            continue

        base_url = entry.get("base_url")
        base_url_str = base_url if isinstance(base_url, str) and base_url.strip() else None
        llm = _instantiate_llm(
            provider_name, api_key=api_key, model=model, base_url=base_url_str,
        )
        if llm is None:
            continue

        label = str(entry.get("label") or "").strip() or pid
        out.append(LLMProfile(
            id=pid, label=label, provider_name=provider_name,
            model=model, llm=llm,
        ))
        seen.add(pid)
    return out


def build_llm_registry_from_config(cfg: dict[str, Any]) -> LLMRegistry:
    """Build the per-session-pickable LLMRegistry.

    The legacy single-block (``llm.default_provider`` + ``llm.openai`` /
    ``llm.anthropic``) becomes a synthesised profile with id
    ``"default"``. New named entries from ``llm.profiles`` follow.

    The registry's ``default_id`` is ``"default"`` when the legacy
    block produced an LLM; otherwise the first named profile (so
    fresh installs that go straight to the new schema still work).
    Empty registry (no LLM at all) returns a registry with no
    default — AgentLoop tolerates that and runs in echo mode.
    """
    profiles: dict[str, LLMProfile] = {}
    default_id: str | None = None

    legacy = build_llm_from_config(cfg)
    if legacy is not None:
        profiles["default"] = LLMProfile(
            id="default",
            label="默认 (config.json)",
            provider_name=type(legacy).__name__.replace("LLM", "").lower(),
            model=getattr(legacy, "model", "") or "",
            llm=legacy,
        )
        default_id = "default"

    for prof in build_llm_profiles_from_config(cfg):
        if prof.id in profiles:
            continue
        profiles[prof.id] = prof
        if default_id is None:
            default_id = prof.id

    return LLMRegistry(profiles=profiles, default_id=default_id)


def _workspace_root_provider() -> Any:
    """Return a callable that yields the daemon's currently-active
    workspace root (or None when none configured).

    Reads ``~/.xmclaw/state.json`` via :class:`WorkspaceManager` on
    every call so the AgentLoop's bash tool picks up live changes the
    user makes via the Web UI Workspace page without a daemon
    restart. Imported lazily to avoid a hard dep cycle for tests
    that build BuiltinTools directly.
    """
    def _provider():
        try:
            from xmclaw.core.workspace import WorkspaceManager
            mgr = WorkspaceManager()
            primary = mgr.get().primary
            return primary.path if primary is not None else None
        except Exception:  # noqa: BLE001
            return None
    return _provider


def _persona_dir_provider(cfg_ref: dict[str, Any]) -> Any:
    """Return a callable that yields the agent's active persona profile
    directory.

    Used by the ``remember`` and ``learn_about_user`` tools so the
    agent can append to MEMORY.md / USER.md inside its own profile.
    Closes over a *reference* to ``cfg`` so a hot config swap (e.g.
    user picks a different profile via the UI) is picked up on the
    next call without rebuilding the BuiltinTools instance.
    """
    def _provider():
        return _resolve_persona_profile_dir(cfg_ref)
    return _provider


def _persona_writeback(app_state_holder: Any) -> Any:
    """Return a callback that rebuilds ``app.state.agent._system_prompt``
    after a persona file write.

    The agent's system prompt is set at construction; once the
    ``remember`` tool appends to MEMORY.md, the very next turn must
    see the new bullet without a daemon restart. This callback rebuilds
    the prompt and assigns it onto the running loop.

    ``app_state_holder`` is a callable returning the FastAPI
    ``app.state`` (or None when the agent isn't wired yet — e.g. tests).
    """
    def _writeback(_basename: str) -> None:
        try:
            state = app_state_holder()
            if state is None:
                return
            agent = getattr(state, "agent", None)
            if agent is None:
                return
            cfg = getattr(state, "config", None) or {}
            profile_dir = _resolve_persona_profile_dir(cfg)
            from xmclaw.core.persona import build_system_prompt
            from xmclaw.core.persona.assembler import clear_cache
            clear_cache()
            tool_specs = []
            tools = getattr(agent, "_tools", None)
            if tools is not None:
                try:
                    tool_specs = tools.list_tools() or []
                except Exception:  # noqa: BLE001
                    tool_specs = []
            ws_root = None
            try:
                from xmclaw.core.workspace import WorkspaceManager
                ws = WorkspaceManager().get()
                if ws.primary is not None:
                    ws_root = Path(ws.primary.path)
            except Exception:  # noqa: BLE001
                ws_root = None
            new_prompt = build_system_prompt(
                profile_dir=profile_dir,
                workspace_dir=ws_root,
                tool_names=[s.name for s in tool_specs],
            )
            agent._system_prompt = new_prompt  # noqa: SLF001
            # B-25: bump the frozen-prompt-snapshot generation so all
            # sessions re-render their system prompt on the next turn.
            # Without this, the static cache would still serve the
            # PRE-edit system prompt — the agent's own ``remember``
            # write wouldn't take effect until a daemon restart.
            try:
                from xmclaw.daemon.agent_loop import bump_prompt_freeze_generation
                bump_prompt_freeze_generation()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 — never let writeback failures
            # break the tool call. Worst case the agent doesn't see its
            # own write until the daemon restarts.
            pass
    return _writeback


def build_tools_from_config(
    cfg: dict[str, Any],
    *,
    approval_service: Any | None = None,
) -> ToolProvider | None:
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

    # Stash the daemon's app.state in module scope so the persona
    # writeback callback can find the running agent. ``build_tools_*``
    # is called both from the lifespan (where app.state.agent is set
    # later) and at agent boot (where it isn't yet) — so we look up
    # lazily on each call.
    def _app_state_holder():
        try:
            from xmclaw.daemon import app as _app_mod
            return getattr(_app_mod, "_LAST_APP_STATE", None)
        except Exception:  # noqa: BLE001
            return None

    builtins = BuiltinTools(
        allowed_dirs=allowed_dirs,
        enable_bash=bool(enable_bash),
        enable_web=bool(enable_web),
        workspace_root_provider=_workspace_root_provider(),
        persona_dir_provider=_persona_dir_provider(cfg),
        persona_writeback=_persona_writeback(_app_state_holder),
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
        provider = builtins  # no extras wired -- skip the composite wrapper
    else:
        from xmclaw.providers.tool.composite import CompositeToolProvider
        provider = CompositeToolProvider(*children)

    # Epic #3: optionally wrap with security guardians
    security_cfg = cfg.get("security", {})
    guardians_cfg = security_cfg.get("guardians", {})
    if guardians_cfg.get("enabled", False):
        from xmclaw.security.tool_guard.engine import ToolGuardEngine
        from xmclaw.security.tool_guard.file_guardian import FilePathToolGuardian
        from xmclaw.security.tool_guard.rule_guardian import RuleBasedToolGuardian
        from xmclaw.security.tool_guard.shell_evasion_guardian import ShellEvasionGuardian
        from xmclaw.security.tool_guard.models import GuardianPolicy
        from xmclaw.providers.tool.guarded import GuardedToolProvider

        engine = ToolGuardEngine(guardians=[
            FilePathToolGuardian(
                sensitive_files=guardians_cfg.get("sensitive_files")
            ),
            RuleBasedToolGuardian(),
            ShellEvasionGuardian(),
        ])

        # Parse ``security.guardians.policy`` — per-severity action
        # mapping (critical/high/medium/low/info -> allow/approve/deny).
        # Unknown severities or actions raise ValueError with a
        # known-set message; we re-raise so bad config surfaces at
        # startup rather than silently reverting to defaults.
        policy_cfg = guardians_cfg.get("policy")
        policy = GuardianPolicy.from_config(policy_cfg)

        provider = GuardedToolProvider(
            provider,
            engine,
            approval_service=approval_service,
            policy=policy,
        )

    return provider


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


def _resolve_persona_profile_dir(cfg: dict[str, Any]) -> Path:
    """Pick the active persona profile directory.

    Replaces the prior ``_load_persona_addendum`` ad-hoc loader. Returns
    a directory path (which may not exist yet — assembler falls back to
    bundled templates). Resolution order:

    1. ``cfg["persona"]["profile_id"]`` → ``~/.xmclaw/persona/profiles/<id>/``
    2. ``cfg["persona"]["text"]`` (inline) → returns a special transient
       directory representing "use bundled templates with this SOUL.md
       overlay". Implemented by writing the inline text to a temp
       SOUL.md inside ``profiles/_inline/`` so the assembler picks it
       up via the normal layer cascade.
    3. Default → ``~/.xmclaw/persona/profiles/default/``
    """
    section = cfg.get("persona") if isinstance(cfg, Mapping) else None
    if isinstance(section, Mapping):
        profile_id = section.get("profile_id")
        if isinstance(profile_id, str) and profile_id.strip():
            stem = profile_id.strip().replace("/", "_").replace("\\", "_")
            return persona_dir().parent / "profiles" / stem
        inline = section.get("text")
        if isinstance(inline, str) and inline.strip():
            inline_dir = persona_dir().parent / "profiles" / "_inline"
            try:
                inline_dir.mkdir(parents=True, exist_ok=True)
                (inline_dir / "SOUL.md").write_text(
                    inline.strip(), encoding="utf-8"
                )
            except OSError:
                pass
            return inline_dir
    return persona_dir().parent / "profiles" / "default"


def build_agent_from_config(
    cfg: dict[str, Any],
    bus: InProcessEventBus,
    *,
    max_hops: int = 20,
    approval_service: Any | None = None,
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
    registry = build_llm_registry_from_config(cfg)
    default_profile = registry.default()
    llm = default_profile.llm if default_profile is not None else None
    if llm is None:
        return None
    tools = build_tools_from_config(cfg, approval_service=approval_service)
    security = cfg.get("security")
    policy_raw = None
    if isinstance(security, Mapping):
        policy_raw = security.get("prompt_injection")
        if not isinstance(policy_raw, str):
            policy_raw = None
    policy = PolicyMode.parse(policy_raw, default=PolicyMode.DETECT_ONLY)
    # Persistent conversation history. Best-effort — if SQLite init
    # fails (read-only fs, weird permissions) we fall back to in-memory
    # only so the daemon still boots.
    session_store: SessionStore | None
    try:
        session_store = SessionStore(default_sessions_db_path())
    except Exception:  # noqa: BLE001
        session_store = None
    # Persona system: assemble system prompt from the 7-file SOUL pack
    # (xmclaw/core/persona). Mirrors OpenClaw / Hermes / QwenPaw layout.
    # The DEFAULT_IDENTITY_LINE is always slot 0 so identity survives
    # third-party endpoints that compress long system prompts.
    from xmclaw.core.persona import (
        build_system_prompt,
        ensure_default_profile,
    )
    profile_dir = _resolve_persona_profile_dir(cfg)
    # Materialize bundled templates on first install so the user can
    # actually edit them (otherwise they only see the prompt output, not
    # the source files). Idempotent — won't overwrite existing files.
    try:
        ensure_default_profile(profile_dir)
    except OSError:
        pass
    workspace_root: Path | None = None
    ws_section = cfg.get("workspace") if isinstance(cfg, Mapping) else None
    if isinstance(ws_section, Mapping):
        ws_path = ws_section.get("path")
        if isinstance(ws_path, str) and ws_path.strip():
            workspace_root = Path(ws_path).expanduser()
    tool_specs = tools.list_tools() if tools is not None else []
    system_prompt = build_system_prompt(
        profile_dir=profile_dir,
        workspace_dir=workspace_root,
        tool_names=[s.name for s in tool_specs],
    )
    # Cross-session memory: B-26 builds a MemoryManager with two
    # providers — the BuiltinFileMemoryProvider (always-on, wraps
    # MEMORY.md / USER.md) plus an external SqliteVecMemory when one
    # builds successfully. Hermes-style: only ONE external provider
    # at a time, builtin is non-removable. Both are best-effort: if
    # init fails the agent stays usable without long-term recall.
    from xmclaw.providers.memory.manager import MemoryManager
    from xmclaw.providers.memory.builtin_file import BuiltinFileMemoryProvider

    memory_manager = MemoryManager(bus=bus)
    # Builtin file provider — backed by the persona profile dir.
    try:
        memory_manager.add_provider(
            BuiltinFileMemoryProvider(
                persona_dir_provider=lambda pd=profile_dir: pd,
            )
        )
    except Exception:  # noqa: BLE001
        pass
    # External provider — selected by ``evolution.memory.provider``
    # (sqlite_vec | hindsight | none). Default sqlite_vec for back-
    # compat. Best-effort throughout: provider init failure falls
    # back to "no external" without breaking agent boot.
    evo_section = (cfg or {}).get("evolution") or {}
    provider_choice = (
        (evo_section.get("memory") or {}).get("provider")
        or "sqlite_vec"
    )
    if provider_choice == "sqlite_vec":
        try:
            external = build_memory_from_config(cfg, bus=bus)
            if external is not None:
                memory_manager.add_provider(external)
        except Exception:  # noqa: BLE001
            pass
    elif provider_choice == "hindsight":
        try:
            from xmclaw.providers.memory.hindsight import HindsightMemoryProvider
            cfg_hs = (evo_section.get("memory") or {}).get("hindsight") or {}
            hs = HindsightMemoryProvider(
                api_key=cfg_hs.get("api_key"),
                base_url=cfg_hs.get("base_url"),
            )
            if hs.is_available():
                memory_manager.add_provider(hs)
            else:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).warning(
                    "memory.hindsight_unavailable — needs api_key + SDK; "
                    "falling back to no external provider",
                )
        except Exception:  # noqa: BLE001
            pass
    elif provider_choice == "supermemory":
        try:
            from xmclaw.providers.memory.supermemory import SupermemoryMemoryProvider
            cfg_sm = (evo_section.get("memory") or {}).get("supermemory") or {}
            sm = SupermemoryMemoryProvider(
                api_key=cfg_sm.get("api_key"),
                base_url=cfg_sm.get("base_url"),
                container_tag=cfg_sm.get("container_tag"),
            )
            if sm.is_available():
                memory_manager.add_provider(sm)
            else:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).warning(
                    "memory.supermemory_unavailable — needs api_key; "
                    "falling back to no external provider",
                )
        except Exception:  # noqa: BLE001
            pass
    elif provider_choice == "mem0":
        try:
            from xmclaw.providers.memory.mem0 import Mem0MemoryProvider
            cfg_m0 = (evo_section.get("memory") or {}).get("mem0") or {}
            m0 = Mem0MemoryProvider(
                api_key=cfg_m0.get("api_key"),
                base_url=cfg_m0.get("base_url"),
                user_id=cfg_m0.get("user_id"),
            )
            if m0.is_available():
                memory_manager.add_provider(m0)
            else:
                from xmclaw.utils.log import get_logger
                get_logger(__name__).warning(
                    "memory.mem0_unavailable — needs api_key; "
                    "falling back to no external provider",
                )
        except Exception:  # noqa: BLE001
            pass
    # provider_choice == "none" → no external provider registered.

    # Keep an opt-out: tests / minimal configs that explicitly set
    # memory.enabled=false get None instead of an empty-but-noisy
    # manager.
    mem_section = (cfg or {}).get("memory") or {}
    if mem_section.get("enabled", True) is False and memory_manager.is_empty:
        memory_arg = None
    else:
        memory_arg = memory_manager if not memory_manager.is_empty else None

    # B-27: if memory providers expose tool schemas, wrap them in a
    # MemoryToolBridge and chain with the agent's existing tools via
    # CompositeToolProvider. Today's BuiltinFile/SqliteVec providers
    # both return [] schemas so this is currently a no-op — but the
    # plumbing is ready for hindsight / supermemory plugins that DO
    # expose recall_memory / synthesize / etc tools.
    if memory_arg is not None and tools is not None:
        try:
            from xmclaw.providers.memory.tool_bridge import MemoryToolBridge
            from xmclaw.providers.tool.composite import CompositeToolProvider
            bridge = MemoryToolBridge(memory_arg)
            if bridge.list_tools():
                tools = CompositeToolProvider(tools, bridge)
        except Exception:  # noqa: BLE001 — bridge failure should not
            # block agent boot
            pass

    # B-40: wire the MemoryManager into BuiltinTools so the unified
    # ``memory_search`` tool surfaces. Walk the composite chain to
    # find the BuiltinTools instance — it might be inside a
    # CompositeToolProvider added by the memory-bridge step above.
    #
    # B-42: also wire the EmbeddingProvider so memory_search embeds
    # the query and pulls real semantic hits (not just keyword) from
    # SqliteVecMemory.
    if memory_arg is not None and tools is not None:
        from xmclaw.providers.tool.builtin import BuiltinTools
        from xmclaw.providers.tool.composite import CompositeToolProvider
        from xmclaw.providers.memory.embedding import build_embedding_provider

        def _walk(p):
            if isinstance(p, BuiltinTools):
                yield p
            elif isinstance(p, CompositeToolProvider):
                for child in getattr(p, "_children", []):
                    yield from _walk(child)

        embedder = None
        try:
            embedder = build_embedding_provider(cfg)
        except Exception:  # noqa: BLE001
            embedder = None

        for bt in _walk(tools):
            try:
                bt.set_memory_manager(memory_arg)
            except Exception:  # noqa: BLE001
                pass
            try:
                if embedder is not None:
                    bt.set_embedder(embedder)
            except Exception:  # noqa: BLE001
                pass

    # B-31: optional token-based compression gate. When set in config
    # ( ``evolution.compression.token_cap`` ), AgentLoop fires
    # compression once the kept history's char/4 estimate crosses
    # this threshold — protects against single-huge-message overruns
    # that the message-count cap can't catch.
    compression_section = (cfg or {}).get("evolution", {}).get("compression") or {}
    raw_token_cap = compression_section.get("token_cap")
    token_cap: int | None
    if isinstance(raw_token_cap, (int, float)) and raw_token_cap > 0:
        token_cap = int(raw_token_cap)
    else:
        token_cap = None

    # B-55: hand the EmbeddingProvider to AgentLoop too — without it
    # cross-session memory prefetch falls back to "most recent items"
    # rather than semantically related ones. The same embedder the
    # indexer + memory_search use. Variable may be unbound if the
    # memory-bridge branch above didn't run; resolve cleanly.
    try:
        agent_embedder = embedder  # noqa: F821 — bound in B-40 branch above
    except NameError:
        agent_embedder = None
    if agent_embedder is None:
        try:
            from xmclaw.providers.memory.embedding import build_embedding_provider
            agent_embedder = build_embedding_provider(cfg)
        except Exception:  # noqa: BLE001
            agent_embedder = None

    # B-93: opt-in LLM-pick top-K memory files. Reads
    # ``evolution.memory.relevant_picker.{enabled,k,max_chars}`` from
    # config.json. Defaults are ALL OFF / conservative — adds one extra
    # LLM call per turn so it stays opt-in until the user decides the
    # extra recall is worth the latency / cost.
    _picker_section = (
        ((cfg.get("evolution") or {}).get("memory") or {})
        .get("relevant_picker") or {}
    )
    _picker_enabled = bool(_picker_section.get("enabled", False))
    _picker_k = int(_picker_section.get("k", 3))
    _picker_max_chars = int(_picker_section.get("max_chars", 4000))

    return AgentLoop(
        llm=llm, bus=bus, tools=tools,
        system_prompt=system_prompt,
        max_hops=max_hops,
        agent_id=cfg.get("agent_id", "agent"),
        prompt_injection_policy=policy,
        session_store=session_store,
        llm_registry=registry,
        memory=memory_arg,
        compression_token_cap=token_cap,
        embedder=agent_embedder,
        relevant_files_picker_enabled=_picker_enabled,
        relevant_files_picker_k=_picker_k,
        relevant_files_max_chars=_picker_max_chars,
    )
