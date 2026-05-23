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
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    # Sprint 3 #5 follow-up: evolution components referenced only by
    # forward-string annotation in the new builder factories. Importing
    # under TYPE_CHECKING keeps daemon boot free of core/journal +
    # core/evolution module loads when the user has reasoning_bank /
    # reflective_mutator disabled (the default).
    from xmclaw.core.evolution.reflective_mutator import ReflectiveMutator
    from xmclaw.core.journal.strategy_bank import StrategyBank
    from xmclaw.core.journal.strategy_distiller import StrategyDistiller

from xmclaw.core.bus import InProcessEventBus
from xmclaw.daemon.agent_loop import AgentLoop
from xmclaw.daemon.llm_registry import LLMProfile, LLMRegistry
from xmclaw.daemon.session_store import SessionStore
from xmclaw.providers.llm.anthropic import AnthropicLLM
from xmclaw.providers.llm.base import LLMProvider
from xmclaw.providers.llm.openai import OpenAILLM
from xmclaw.providers.llm.openrouter import OpenRouterLLM
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
# B-386: ``openrouter`` ranks AFTER the two native providers so a user
# who has both an Anthropic / OpenAI key and an OpenRouter key keeps
# hitting the native API by default (cheaper + lower latency); they
# opt into OpenRouter explicitly via the profiles array or by clearing
# the native api_key.
_PROVIDER_ORDER: tuple[str, ...] = ("anthropic", "openai", "openrouter")


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


# Sprint 0 multi-model routing: rough tier inference from model name
# strings. Used when the user didn't explicitly set ``tier`` in their
# profile config. Conservative — when in doubt return "balanced".
def _infer_tier_from_model(model: str) -> str:
    """Heuristic tier mapping based on common Anthropic / OpenAI /
    open-source model name fragments."""
    m = (model or "").lower()
    if not m:
        return "balanced"
    # Fast tier — small chitchat models
    if any(x in m for x in (
        "haiku", "mini", "gpt-3.5", "phi", "qwen-7b", "qwen2-7b",
        "llama-3-8b", "llama3-8b", "deepseek-v2-lite",
        "moonshot-v1-8k", "yi-6b", "gemma-2b", "gemma-7b",
        "8b-instruct", "tinyllama",
    )):
        return "fast"
    # Strong tier — long-chain reasoning heavyweights
    if any(x in m for x in (
        "opus", "gpt-4.1", "gpt-4-turbo", "kimi-k2", "qwen-max",
        "llama-3-405b", "llama3-405b", "deepseek-v3", "deepseek-r1",
        "claude-opus", "o1", "o3",
    )):
        return "strong"
    # Vision tier — vision-tuned for GUI / image grounding
    if any(x in m for x in (
        "sonnet", "gpt-4o", "gpt-4.5",
        "ui-tars", "qwen2-vl", "qwen-vl", "vl-",
        "cogvlm", "showui",
    )):
        # Most "sonnet" / "4o" are both balanced AND vision-capable.
        # We bucket them as "vision" so vision turns pick them first;
        # fallback chain catches non-vision needs.
        return "vision"
    return "balanced"


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
        # B-320: respect a per-provider ``prompt_cache_enabled`` switch.
        # ``None`` keeps OpenAILLM's auto-detect (Moonshot / Zhipu on,
        # OpenAI / DeepSeek off); explicit True/False overrides.
        raw_pc = pcfg.get("prompt_cache_enabled")
        prompt_cache_enabled: bool | None = (
            bool(raw_pc) if isinstance(raw_pc, bool) else None
        )
        # Wave-27 fix-6: optional explicit context-window override.
        # When set, takes priority over the static lookup table in
        # _provider_profiles — gives users an escape hatch for any
        # endpoint / model the static table doesn't know about.
        raw_ctx = pcfg.get("context_length")
        ctx_len_override: int | None = None
        if isinstance(raw_ctx, int) and raw_ctx > 0:
            ctx_len_override = raw_ctx
        elif isinstance(raw_ctx, str) and raw_ctx.strip().isdigit():
            ctx_len_override = int(raw_ctx.strip())
        # Epic #27 sweep #14 (2026-05-19): per-provider max_tokens
        # override. Reads ``llm.<provider>.max_tokens`` from config
        # (or the per-profile ``profiles[*].max_tokens``). When unset
        # the provider falls back to its internal default (8192 for
        # AnthropicLLM, the SDK default for others).
        raw_max = pcfg.get("max_tokens")
        max_tokens_override: int | None = None
        if isinstance(raw_max, int) and raw_max > 0:
            max_tokens_override = raw_max
        elif isinstance(raw_max, str) and raw_max.strip().isdigit():
            max_tokens_override = int(raw_max.strip())
        if provider_name == "anthropic":
            return AnthropicLLM(
                api_key=api_key, model=model, base_url=base_url or None,
                context_length=ctx_len_override,
                max_tokens=max_tokens_override,
            )
        if provider_name == "openai":
            return OpenAILLM(
                api_key=api_key, model=model, base_url=base_url or None,
                prompt_cache_enabled=prompt_cache_enabled,
                context_length=ctx_len_override,
            )
        if provider_name == "openrouter":
            # B-386: OpenRouterLLM injects HTTP-Referer + X-Title
            # attribution headers and falls back to OpenRouter's base
            # URL when ``base_url`` is empty. Cache auto-detect runs on
            # the model prefix (anthropic/* + openai/* → on).
            return OpenRouterLLM(
                api_key=api_key, model=model, base_url=base_url or None,
                prompt_cache_enabled=prompt_cache_enabled,
                context_length=ctx_len_override,
            )

    return None


def _default_model_for(provider_name: str) -> str:
    """Last-ditch default — used only when the config omits model."""
    return {
        "anthropic": "claude-haiku-4-5-20251001",
        "openai": "gpt-4o-mini",
        # B-386: OpenRouter's sweet spot for coding agents (good
        # price/quality, broad tool support) as of 2026-Q2.
        "openrouter": "anthropic/claude-sonnet-4",
    }.get(provider_name, "")


def _instantiate_llm(
    provider_name: str,
    *,
    api_key: str,
    model: str,
    base_url: str | None,
    prompt_cache_enabled: bool | None = None,
) -> LLMProvider | None:
    """Construct one LLMProvider from already-resolved config values.

    Centralised here so both the legacy single-block path and the new
    profiles-array path build providers identically. Returns ``None``
    when ``provider_name`` is unknown — callers skip the entry rather
    than crashing the whole registry.

    ``prompt_cache_enabled`` (B-320): forwarded only to OpenAILLM since
    AnthropicLLM caches unconditionally (B-245). ``None`` keeps the
    provider's auto-detect — explicit True/False overrides.
    """
    if provider_name == "anthropic":
        return AnthropicLLM(api_key=api_key, model=model, base_url=base_url or None)
    if provider_name == "openai":
        return OpenAILLM(
            api_key=api_key, model=model, base_url=base_url or None,
            prompt_cache_enabled=prompt_cache_enabled,
        )
    if provider_name == "openrouter":
        # B-386: same shape as openai but with OpenRouter's base URL
        # default + HTTP-Referer / X-Title attribution headers.
        return OpenRouterLLM(
            api_key=api_key, model=model, base_url=base_url or None,
            prompt_cache_enabled=prompt_cache_enabled,
        )
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

    B-146 api_key inheritance: when a profile entry leaves ``api_key``
    blank, fall back to the SAME provider's legacy block
    (``llm.<provider>.api_key``) before giving up. This kills the
    "Profile 创建强制重填 api_key" UX wart — user fills the key once
    in 设置 / 高级配置, and every same-provider profile inherits it.
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
        # B-146: inherit from legacy same-provider block when still
        # empty. Only honors the literal legacy block (cfg-driven
        # inheritance), NOT the env-var / secrets path — those flow
        # through the legacy build path on its own. This keeps
        # profile loading deterministic with the cfg dict alone.
        if not api_key.strip():
            legacy_pcfg = llm_section.get(provider_name)
            if isinstance(legacy_pcfg, dict):
                inherited = legacy_pcfg.get("api_key")
                if isinstance(inherited, str) and inherited.strip():
                    api_key = inherited
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
        # B-146: inherit base_url from legacy same-provider block when
        # not set on the profile (keeps OpenAI-compatible endpoint URL
        # like MiniMax / DeepSeek consistent across profiles).
        if base_url_str is None:
            legacy_pcfg = llm_section.get(provider_name)
            if isinstance(legacy_pcfg, dict):
                inherited_url = legacy_pcfg.get("base_url")
                if isinstance(inherited_url, str) and inherited_url.strip():
                    base_url_str = inherited_url
        # B-320: per-profile prompt_cache_enabled with legacy-block
        # inheritance (matches the api_key / base_url pattern). None
        # means "let OpenAILLM auto-detect from base_url + model".
        raw_pc_profile = entry.get("prompt_cache_enabled")
        prompt_cache_enabled: bool | None = (
            bool(raw_pc_profile) if isinstance(raw_pc_profile, bool) else None
        )
        if prompt_cache_enabled is None:
            legacy_pcfg = llm_section.get(provider_name)
            if isinstance(legacy_pcfg, dict):
                raw_inherited = legacy_pcfg.get("prompt_cache_enabled")
                if isinstance(raw_inherited, bool):
                    prompt_cache_enabled = raw_inherited
        llm = _instantiate_llm(
            provider_name, api_key=api_key, model=model,
            base_url=base_url_str,
            prompt_cache_enabled=prompt_cache_enabled,
        )
        if llm is None:
            continue

        label = str(entry.get("label") or "").strip() or pid
        # Sprint 0 multi-model routing: pull the explicit tier from
        # config; default to "balanced" so existing configs keep
        # working without change.
        raw_tier = str(entry.get("tier") or "").strip().lower()
        tier = raw_tier if raw_tier in ("fast", "balanced", "strong", "vision") else "balanced"
        out.append(LLMProfile(
            id=pid, label=label, provider_name=provider_name,
            model=model, llm=llm, tier=tier,
        ))
        seen.add(pid)
    return out


def build_llm_registry_from_config(cfg: dict[str, Any]) -> LLMRegistry:
    """Build the per-session-pickable LLMRegistry.

    The single-block (``llm.openai`` / ``llm.anthropic``) becomes a
    synthesised profile with id ``"default"`` — provider auto-picked
    from the first block with a non-empty ``api_key`` (anthropic
    preferred). New named entries from ``llm.profiles`` follow.
    (Pre-B-304: the obsolete ``llm.default_provider`` field had no
    effect since B-146; it was removed from config.example.json.)

    Default selection order (B-146):
      1. ``llm.default_profile_id`` if it points to an existing profile.
         Lets the user pin a NAMED profile as the daemon-wide default,
         the missing knob users kept asking for.
      2. ``"default"`` (the legacy block) when present.
      3. First named profile when no legacy block.
      4. None → AgentLoop runs in echo mode.

    Empty registry (no LLM at all) returns a registry with no
    default — AgentLoop tolerates that and runs in echo mode.
    """
    profiles: dict[str, LLMProfile] = {}

    legacy = build_llm_from_config(cfg)
    if legacy is not None:
        # B-146: derive a useful label instead of the opaque "默认 (config.json)".
        # Show "<provider>/<model>" so the chat picker can surface what
        # the daemon is actually wired to without the user reading
        # config.json.
        legacy_provider = type(legacy).__name__.replace("LLM", "").lower()
        legacy_model = getattr(legacy, "model", "") or ""
        legacy_label = (
            f"{legacy_provider}/{legacy_model}" if legacy_model else legacy_provider
        )
        profiles["default"] = LLMProfile(
            id="default",
            label=legacy_label,
            provider_name=legacy_provider,
            model=legacy_model,
            llm=legacy,
            tier=_infer_tier_from_model(legacy_model),
        )

    for prof in build_llm_profiles_from_config(cfg):
        if prof.id in profiles:
            continue
        profiles[prof.id] = prof

    # B-146: explicit default_profile_id wins over the legacy fallback.
    default_id: str | None = None
    llm_section = cfg.get("llm") if isinstance(cfg, dict) else None
    if isinstance(llm_section, dict):
        explicit = llm_section.get("default_profile_id")
        if isinstance(explicit, str) and explicit.strip() in profiles:
            default_id = explicit.strip()
    if default_id is None and "default" in profiles:
        default_id = "default"
    if default_id is None and profiles:
        default_id = next(iter(profiles))

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


def _workspace_manager_provider() -> Any:
    """B-331: callable () -> WorkspaceManager | None for the
    BuiltinTools write-path containment audit. Distinct from
    :func:`_workspace_root_provider` — that one returns just the
    primary root's Path (used by bash CWD); this one returns the
    full manager so the tool can call ``resolve_path_to_root`` to
    check membership across all configured roots, not only primary.
    """
    def _provider():
        try:
            from xmclaw.core.workspace import WorkspaceManager
            return WorkspaceManager()
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
                backend_label=_resolve_backend_label(cfg),
            )
            agent._system_prompt = new_prompt  # noqa: SLF001
            # B-25: bump the frozen-prompt-snapshot generation so all
            # sessions re-render their system prompt on the next turn.
            # Without this, the static cache would still serve the
            # PRE-edit system prompt — the agent's own ``remember``
            # write wouldn't take effect until a daemon restart.
            try:
                from xmclaw.daemon.prompt_builder import bump_prompt_freeze_generation
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
    bus: "InProcessEventBus | None" = None,
    approval_service: Any | None = None,
    auditor: Any | None = None,
    session_store: Any | None = None,
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
        return BuiltinTools(session_store=session_store)
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
    # 2026-05-14 default-flip: browser tools are headless DOM-scoped,
    # much safer than enable_bash (which is also default-on). Lazy
    # playwright import means daemons without ``playwright install
    # chromium`` log a startup skip and continue cleanly — the seven
    # browser_* tools just won't list.
    enable_browser = tools_section.get("enable_browser", True)
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

    # B-198 Phase 3: tools also route through PersonaStore when
    # available so user/agent-driven persona edits update the DB
    # (truth) before the disk file (cache). Provider is a callable
    # so tools fetch the latest store reference each call — the
    # store is created in lifespan AFTER the agent, so a static
    # capture would be None.
    def _persona_store_provider() -> Any:
        # B-340 (audit pass-2 #5): ``st`` is Starlette's ``State``
        # object (supports ``__getattr__`` / ``__setattr__``, NOT a
        # ``.get`` method). Pre-B-340 ``st.get("persona_store")``
        # raised AttributeError; the call site in BuiltinTools
        # wrapped it in try/except → ``store`` was always None →
        # the entire B-198 Phase-3 PersonaStore (DB-as-truth)
        # wiring was silently inert in production. Every persona-
        # mutating tool fell back to the legacy markdown path,
        # ignoring the rendered-cache + upsert refactor.
        # ``getattr(..., None)`` matches the sibling
        # ``_persona_writeback`` helper at line ~609.
        st = _app_state_holder()
        if st is None:
            return None
        return getattr(st, "persona_store", None)

    # 2026-05-12: voice providers — wire ``voice.stt`` / ``voice.tts``
    # config blocks into WhisperSTT / EdgeTTS instances and hand them
    # to BuiltinTools so ``voice_transcribe`` / ``voice_synthesize``
    # tools advertise themselves. Pre-this the provider classes existed
    # but no callsite constructed them — voice tools were dead code.
    # Optional + lazy: missing extras (``faster-whisper`` / ``edge-tts``)
    # surface at construction time as None (tools hide) rather than at
    # daemon boot, so installs without the [voice] extra still boot.
    voice_cfg = cfg.get("voice") or {}
    _stt_provider: Any = None
    _tts_provider: Any = None
    stt_section = voice_cfg.get("stt") if isinstance(voice_cfg, dict) else None
    if isinstance(stt_section, dict):
        try:
            from xmclaw.providers.voice.whisper import WhisperSTT
            _stt_provider = WhisperSTT(
                model_name=str(stt_section.get("model") or "tiny"),
                device=str(stt_section.get("device") or "cpu"),
                compute_type=str(stt_section.get("compute_type") or "int8"),
                language=stt_section.get("language"),
            )
        except Exception:  # noqa: BLE001
            # Provider construction itself shouldn't fail (real model
            # load is deferred to first transcribe). If it does, leave
            # provider None — voice_transcribe just won't list.
            _stt_provider = None
    tts_section = voice_cfg.get("tts") if isinstance(voice_cfg, dict) else None
    if isinstance(tts_section, dict):
        try:
            from xmclaw.providers.voice.edge_tts import EdgeTTS
            _tts_provider = EdgeTTS(
                voice=str(tts_section.get("voice") or "zh-CN-XiaoxiaoNeural"),
                rate=str(tts_section.get("rate") or "+0%"),
                volume=str(tts_section.get("volume") or "+0%"),
            )
        except Exception:  # noqa: BLE001
            _tts_provider = None

    # Sprint 0 Track B: undo cabinet for destructive file ops.
    # Opt-out via tools.undo_cabinet.enabled=false (default ON).
    _undo_cab = None
    _undo_cfg = tools_section.get("undo_cabinet") or {}
    if (
        not isinstance(_undo_cfg, dict)
        or _undo_cfg.get("enabled", True)
    ):
        try:
            from xmclaw.security.undo_cabinet import UndoCabinet
            _window = (
                float(_undo_cfg.get("window_s", 1800))
                if isinstance(_undo_cfg, dict) else 1800.0
            )
            _undo_cab = UndoCabinet(window_s=_window)
        except Exception:  # noqa: BLE001 — undo is nice-to-have, not load-bearing
            _undo_cab = None

    # Wave-27 fix-LAT8: search backend config lookup. Closure pins
    # the cfg ref so a runtime cfg reload (which mutates the dict in
    # place) is automatically reflected in the next web_search call.
    def _search_cfg_getter() -> dict[str, Any]:
        try:
            evo = (cfg or {}).get("evolution") or {}
            sec = evo.get("search") or {}
            return sec if isinstance(sec, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    # Live Canvas / A2UI: callback that fires CANVAS_ARTIFACT_* events
    # onto the bus so the frontend reducer can surface live mutations.
    def _canvas_listener(event_type, payload):
        if bus is None:
            return
        from xmclaw.core.bus import make_event
        try:
            event = make_event(
                session_id="_system",
                agent_id="canvas",
                type=event_type,
                payload=payload,
            )
            # Fire-and-forget; don't block the tool call over bus back-pressure.
            import asyncio
            asyncio.create_task(bus.publish(event))
        except Exception:
            pass

    builtins = BuiltinTools(
        allowed_dirs=allowed_dirs,
        enable_bash=bool(enable_bash),
        enable_web=bool(enable_web),
        workspace_root_provider=_workspace_root_provider(),
        workspace_manager_provider=_workspace_manager_provider(),
        persona_dir_provider=_persona_dir_provider(cfg),
        persona_writeback=_persona_writeback(_app_state_holder),
        persona_store_provider=_persona_store_provider,
        stt_provider=_stt_provider,
        tts_provider=_tts_provider,
        undo_cabinet=_undo_cab,
        search_config_getter=_search_cfg_getter,
        canvas_listener=_canvas_listener,
        session_store=session_store,
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

    # 2026-05-12: computer-use tools (mouse / keyboard / screen capture /
    # window control). Default OFF — this is the most dangerous tool
    # surface XMclaw exposes: the agent literally drives the user's GUI.
    # Opt-in via ``tools.computer_use.enabled = true``. Each tool also
    # degrades when ``pyautogui`` isn't installed, surfacing an install
    # hint instead of crashing the daemon. See xmclaw/providers/tool/
    # computer_use.py module docstring for the full safety model.
    # 2026-05-14 default-flip: same posture as enable_browser — lazy
    # import means daemons without pyautogui/mss/pygetwindow log skip
    # and continue cleanly. Explicit ``enabled: false`` still opts out.
    cu_cfg = tools_section.get("computer_use") or {}
    cu_enabled = (
        cu_cfg.get("enabled", True) if isinstance(cu_cfg, dict) else False
    )
    if cu_enabled:
        try:
            from xmclaw.providers.tool.computer_use import ComputerUseTools
            children.append(ComputerUseTools(
                screenshot_dir=cu_cfg.get("screenshot_dir") or None,
                base64_size_cap=int(
                    cu_cfg.get("base64_size_cap", 512 * 1024),
                ),
            ))
        except Exception:  # noqa: BLE001 — never block boot over an optional tool
            pass

    # 2026-05-12: media tools (microphone / camera / live audio
    # playback). Same opt-in posture as computer_use — DEFAULT OFF.
    # ``tools.media.enabled = true`` must be set explicitly. Voice
    # providers (stt/tts) are shared with BuiltinTools so
    # ``voice_listen`` / ``speak`` see the same WhisperSTT / EdgeTTS
    # instances as ``voice_transcribe`` / ``voice_synthesize``.
    # 2026-05-14 default-flip: lazy import + structured error on missing
    # cv2/sounddevice means daemons without the [media] extra boot clean.
    media_cfg = tools_section.get("media") or {}
    media_enabled = (
        media_cfg.get("enabled", True)
        if isinstance(media_cfg, dict) else False
    )
    if media_enabled:
        try:
            from xmclaw.providers.tool.media import MediaTools
            children.append(MediaTools(
                media_dir=media_cfg.get("media_dir") or None,
                stt_provider=_stt_provider,
                tts_provider=_tts_provider,
                base64_size_cap=int(
                    media_cfg.get("base64_size_cap", 512 * 1024),
                ),
            ))
        except Exception:  # noqa: BLE001
            pass

    # B-389 Sprint 2: optionally bridge Composio's 7000+ pre-integrated
    # tools (Gmail / Slack / GitHub / Notion / Linear / HubSpot / …) into
    # the agent's toolset. Block is independently gated; default off so a
    # user without a Composio account or the optional extra installed
    # boots cleanly. Missing api_key surfaces as a clear ConfigError —
    # silently skipping a misconfigured-but-enabled block would hide a
    # real user mistake. Lazy SDK import: ``composio`` only loads when
    # the user actually triggers a list_tools / invoke call, so a daemon
    # with ``enabled=False`` doesn't pull the ``composio-core`` extra.
    composio_cfg = tools_section.get("composio")
    if isinstance(composio_cfg, dict) and composio_cfg.get("enabled"):
        api_key = composio_cfg.get("api_key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ConfigError(
                "'tools.composio.enabled' is true but "
                "'tools.composio.api_key' is empty. Get one at "
                "https://app.composio.dev or set XMC__tools__composio__api_key."
            )
        apps_raw = composio_cfg.get("apps") or []
        if not isinstance(apps_raw, list):
            raise ConfigError(
                f"'tools.composio.apps' must be a list, got {type(apps_raw).__name__}"
            )
        from xmclaw.providers.tool.composio import ComposioToolProvider
        children.append(ComposioToolProvider(
            api_key=api_key,
            entity_id=str(composio_cfg.get("entity_id") or "default"),
            apps=[a for a in apps_raw if isinstance(a, str) and a.strip()],
            cache_ttl_s=float(composio_cfg.get("cache_ttl_s", 300.0)),
        ))

    # Sprint 2 Wave 15: calendar write-back tool. Hooked to the same
    # ICS file CalendarReminderTrigger reads, so created events are
    # picked up by the trigger's next read (≤60s cache TTL).
    proactive_cfg = (
        (cfg.get("cognition") or {}).get("proactive", {})
        if isinstance(cfg, dict) else {}
    )
    ics_path = (
        proactive_cfg.get("calendar_ics_path")
        if isinstance(proactive_cfg, dict) else None
    )
    if isinstance(ics_path, str) and ics_path.strip():
        from xmclaw.providers.tool.calendar import CalendarToolProvider
        children.append(CalendarToolProvider(ics_path=ics_path.strip()))

    # CodebaseIndex tool provider (Jarvis Phase J1).
    # Wired when tools.codebase.enabled is true (default true).
    codebase_cfg = tools_section.get("codebase") or {}
    if isinstance(codebase_cfg, dict) and codebase_cfg.get("enabled", True):
        try:
            from xmclaw.cognition.codebase_index import CodebaseStore, CodebaseToolProvider
            from xmclaw.providers.memory.embedding import build_embedding_provider
            from xmclaw.utils.paths import data_dir
            store_path = data_dir() / "v2" / "codebase" / "index.db"
            codebase_store = CodebaseStore(
                store_path,
                embedding_dim=None,  # lazy-init on first embedding
            )
            embedder = build_embedding_provider(cfg)
            children.append(CodebaseToolProvider(
                store=codebase_store,
                embedder=embedder,
            ))
        except Exception:  # noqa: BLE001
            # Lazy-fail: if codebase tools can't load (missing deps,
            # bad config), daemon still boots — the tools just don't list.
            pass

    if len(children) == 1:
        provider = builtins  # no extras wired -- skip the composite wrapper
    else:
        from xmclaw.providers.tool.composite import CompositeToolProvider
        provider = CompositeToolProvider(*children)

    # 2026-05-12 Batch B.2: ErrorAwareRetryProvider — LLM-guided one-shot
    # fixup layer for SEMANTIC tool failures (wrong args, wrong tool).
    # Composes ON TOP of the existing B-17 transient retry in hop_loop.
    # Default ON (zero regression: on success path it's a passthrough;
    # on failure it tries one fixup, returns original on any issue).
    retry_aware_cfg = tools_section.get("retry_aware") or {}
    if (
        not isinstance(retry_aware_cfg, dict)
        or retry_aware_cfg.get("enabled", True)
    ):
        try:
            from xmclaw.providers.tool.retry_aware import (
                ErrorAwareRetryProvider,
            )
            # llm is injected by build_agent_from_config when this
            # function gets called by it; the build_tools-only path
            # gives None and the wrapper is a passthrough on llm=None.
            llm_for_retry = None  # filled in by build_agent_from_config
            provider = ErrorAwareRetryProvider(
                provider,
                llm=llm_for_retry,
                timeout_s=float(
                    retry_aware_cfg.get("timeout_s", 8.0)
                    if isinstance(retry_aware_cfg, dict) else 8.0
                ),
                enabled=True,
            )
        except Exception:  # noqa: BLE001
            pass

    # 2026-05-12 Batch C.1: SubagentToolProvider — ephemeral parallel
    # fanout. Kimi K2.6 agent swarm pattern. Adds the
    # ``parallel_subagents`` tool to the catalogue. Off by default —
    # opt-in via tools.subagent_fanout.enabled.
    # 2026-05-14 default-flip: parallel_subagents tool is pure-logic
    # (no external services), safe to default on.
    subagent_cfg = tools_section.get("subagent_fanout") or {}
    if isinstance(subagent_cfg, dict) and subagent_cfg.get("enabled", True):
        try:
            from xmclaw.providers.tool.builtin_subagent import (
                SubagentToolProvider,
            )
            from xmclaw.providers.tool.composite import CompositeToolProvider
            subagent_provider = SubagentToolProvider(
                llm=None,  # plumbed in by build_agent_from_config
                tools=None,  # plumbed in by build_agent_from_config
                max_hops_per_subagent=int(
                    subagent_cfg.get("max_hops_per_subagent", 6)
                ),
                max_concurrency=int(
                    subagent_cfg.get("max_concurrency", 4)
                ),
                fanout_timeout_s=float(
                    subagent_cfg.get("fanout_timeout_s", 120.0)
                ),
                per_subagent_timeout_s=float(
                    subagent_cfg.get("per_subagent_timeout_s", 45.0)
                ),
                enabled=True,
            )
            provider = CompositeToolProvider(provider, subagent_provider)
        except Exception:  # noqa: BLE001
            pass

    # Epic #3: optionally wrap with security guardians
    security_cfg = cfg.get("security", {})
    guardians_cfg = security_cfg.get("guardians", {})
    if guardians_cfg.get("enabled", True):
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
            auditor=auditor,
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


_RUNTIME_BACKENDS: dict[str, str] = {
    "local": "local",
    "process": "process",
    "docker": "docker",   # B-385: dispatched via _build_docker_runtime
}


def _build_docker_runtime(rt_section: dict[str, Any]) -> SkillRuntime:
    """B-385 helper: parse + validate ``runtime.docker.*`` sub-section
    and construct ``DockerSkillRuntime``. Lazy-imports the runtime so
    a daemon that doesn't enable docker never has to load the
    ``xmclaw.providers.runtime.docker`` module (which itself lazy-imports
    the docker SDK only on first ``fork``).
    """
    from xmclaw.providers.runtime.docker import DockerSkillRuntime

    docker_cfg = rt_section.get("docker") or {}
    if not isinstance(docker_cfg, dict):
        raise ConfigError(
            f"'runtime.docker' must be an object, got "
            f"{type(docker_cfg).__name__}"
        )
    kwargs: dict[str, Any] = {}

    def _expect_str(key: str) -> None:
        if key in docker_cfg:
            v = docker_cfg[key]
            if not isinstance(v, str):
                raise ConfigError(
                    f"'runtime.docker.{key}' must be a string, got "
                    f"{type(v).__name__}"
                )
            kwargs[key] = v

    def _expect_int(key: str) -> None:
        if key in docker_cfg:
            v = docker_cfg[key]
            # bool is an int subclass — reject explicitly so True/False
            # don't sneak in as 1/0 microseconds.
            if isinstance(v, bool) or not isinstance(v, int):
                raise ConfigError(
                    f"'runtime.docker.{key}' must be an int, got "
                    f"{type(v).__name__}"
                )
            kwargs[key] = v

    def _expect_bool(key: str) -> None:
        if key in docker_cfg:
            v = docker_cfg[key]
            if not isinstance(v, bool):
                raise ConfigError(
                    f"'runtime.docker.{key}' must be a bool, got "
                    f"{type(v).__name__}"
                )
            kwargs[key] = v

    _expect_str("image")
    _expect_str("network_mode")
    _expect_str("mem_limit")
    _expect_int("cpu_quota")
    _expect_int("cpu_period")
    _expect_bool("read_only")
    if "tmpfs" in docker_cfg:
        v = docker_cfg["tmpfs"]
        if not isinstance(v, dict) or not all(
            isinstance(k, str) and isinstance(val, str)
            for k, val in v.items()
        ):
            raise ConfigError(
                "'runtime.docker.tmpfs' must be a dict of str → str"
            )
        kwargs["tmpfs"] = v
    if "timeout_s" in docker_cfg:
        v = docker_cfg["timeout_s"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError(
                f"'runtime.docker.timeout_s' must be a number, got "
                f"{type(v).__name__}"
            )
        kwargs["timeout_s"] = float(v)
    return DockerSkillRuntime(**kwargs)


def build_skill_runtime_from_config(cfg: dict[str, Any]) -> SkillRuntime:
    """Return a ``SkillRuntime`` picked from ``cfg['runtime']``.

    Config shape::

        {
          "runtime": {
            "backend": "local" | "process" | "docker",
            "docker": {                   # only when backend == "docker"
              "image": "python:3.10-slim",
              "network_mode": "none",
              "mem_limit": "512m",
              "cpu_quota": 50000,
              "cpu_period": 100000,
              "read_only": true,
              "tmpfs": {"/tmp": "size=100M"},
              "timeout_s": 30
            }
          }
        }

    Default is ``"local"`` — the in-process runtime is fine for dev and
    for conformance tests that assume a fast startup. Production
    deployments should set ``"process"`` for subprocess isolation OR
    ``"docker"`` (B-385) for the first runtime where ``manifest.permissions_*``
    are kernel-enforced rather than advisory.

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
    if backend not in _RUNTIME_BACKENDS:
        known = ", ".join(sorted(_RUNTIME_BACKENDS))
        raise ConfigError(
            f"'runtime.backend' must be one of {{{known}}}, got {backend!r}"
        )
    if backend == "docker":
        return _build_docker_runtime(rt_section)
    if backend == "process":
        return ProcessSkillRuntime()
    return LocalSkillRuntime()


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


def _resolve_backend_label(cfg: dict[str, Any] | None) -> str | None:
    """Return the active LLM backend rendered as
    ``"<provider>/<model> (<label>)"`` for ground-truth injection into
    the system prompt.

    Wave-27 fix-LAT6: without this, the agent answers "what model are
    you" by hallucinating — Kimi's /coding endpoint is an Anthropic-
    protocol-compatible shim that spoofs Claude-shaped responses, so
    the model self-reports as "Claude 3.5 Sonnet" even though the real
    backend is ``kimi k2.6``. Returning a structured label here lets
    ``build_system_prompt`` inject a "## 当前后端" section that the
    DEFAULT_IDENTITY_LINE explicitly tells the agent to consult.

    Resolution order:
      1. Newer profile-id config: ``llm.default_profile_id`` →
         match in ``llm.profiles[]`` → "<provider>/<model> (<label>)".
      2. Legacy top-level block: ``llm.default_provider`` →
         ``llm.<provider>.default_model`` → "<provider>/<model>".
      3. None when neither resolves — DEFAULT_IDENTITY_LINE has a
         fallback ("I don't know which backend is active").
    """
    llm_section = (cfg or {}).get("llm") or {}
    default_id = llm_section.get("default_profile_id")
    profiles = llm_section.get("profiles") or []
    if default_id and isinstance(profiles, list):
        for p in profiles:
            if isinstance(p, dict) and p.get("id") == default_id:
                model = p.get("model") or "?"
                label = p.get("label") or default_id
                provider = p.get("provider") or "?"
                return f"{provider}/{model} ({label})"
    provider = llm_section.get("default_provider")
    if provider:
        sub = llm_section.get(provider) or {}
        model = sub.get("default_model")
        if model:
            return f"{provider}/{model}"
    return None


def build_agent_from_config(
    cfg: dict[str, Any],
    bus: InProcessEventBus,
    *,
    max_hops: int | None = None,
    approval_service: Any | None = None,
    cognitive_state: Any | None = None,
    auditor: Any | None = None,
    perception_bus: Any | None = None,
) -> AgentLoop | None:
    """Assemble an AgentLoop from config. Returns None if no LLM is set.

    Wires both ``llm`` and ``tools`` sections. A config with an LLM
    but no tools section produces a tool-less AgentLoop (still usable
    for pure-chat scenarios). A config with tools but no LLM still
    returns None — tools without an agent have no caller.

    Epic #14: ``security.prompt_injection`` (string: ``detect_only`` /
    ``redact`` / ``block``) is read here and handed to the loop. Missing
    or unrecognised values fall back to ``detect_only``.

    B-190: ``max_hops`` is now configurable via ``cfg.agent.max_hops``
    (default 40). Audit-style tasks that fan out to many list_dir /
    file_read calls were silently capped at the old default of 20 and
    crashed with empty text. Explicit ``max_hops`` kwarg still wins
    over the config (used by tests).
    """
    if max_hops is None:
        agent_cfg = cfg.get("agent")
        if isinstance(agent_cfg, Mapping):
            try:
                max_hops = int(agent_cfg.get("max_hops", 40))
            except (TypeError, ValueError):
                max_hops = 40
        else:
            max_hops = 40
        if max_hops < 1:
            max_hops = 40
    # Persistent conversation history — create early so it can be wired
    # into BuiltinTools (read_conversation_history tool).
    session_store: SessionStore | None
    try:
        session_store = SessionStore(default_sessions_db_path())
    except Exception:  # noqa: BLE001
        session_store = None
    registry = build_llm_registry_from_config(cfg)
    default_profile = registry.default()
    llm = default_profile.llm if default_profile is not None else None
    if llm is None:
        return None
    tools = build_tools_from_config(
        cfg, bus=bus, approval_service=approval_service, auditor=auditor,
        session_store=session_store,
    )
    # 2026-05-12 Batch B.2: plumb the LLM into the ErrorAwareRetryProvider
    # wrapper. The retry wrapper was constructed in build_tools_from_config
    # before the LLM existed; now it does — wire it.
    try:
        from xmclaw.providers.tool.retry_aware import ErrorAwareRetryProvider
        cur = tools
        while cur is not None:
            if isinstance(cur, ErrorAwareRetryProvider):
                cur.set_llm(llm)
                break
            cur = getattr(cur, "_inner", None)
    except Exception:  # noqa: BLE001
        pass

    # 2026-05-12 Batch C.1: plumb the LLM + inner tools into the
    # SubagentToolProvider so the ``parallel_subagents`` tool can drive
    # ephemeral sub-LLM runs that share the parent agent's tool surface
    # (excluding fanout itself — blocked at runtime to prevent nesting).
    try:
        from xmclaw.providers.tool.builtin_subagent import (
            SubagentToolProvider,
        )
        _stack: list[Any] = [tools]
        while _stack:
            cur = _stack.pop()
            if isinstance(cur, SubagentToolProvider):
                cur.set_llm(llm)
                cur.set_tools(tools)
                break
            for attr in ("_inner", "_providers", "_children"):
                v = getattr(cur, attr, None)
                if v is None:
                    continue
                if isinstance(v, (list, tuple)):
                    _stack.extend(v)
                else:
                    _stack.append(v)
    except Exception:  # noqa: BLE001
        pass
    security = cfg.get("security")
    policy_raw = None
    if isinstance(security, Mapping):
        policy_raw = security.get("prompt_injection")
        if not isinstance(policy_raw, str):
            policy_raw = None
    policy = PolicyMode.parse(policy_raw, default=PolicyMode.DETECT_ONLY)
    # Persona system: assemble system prompt from the 7-file SOUL pack
    # (xmclaw/core/persona). Mirrors OpenClaw / Hermes / QwenPaw layout.
    # The DEFAULT_IDENTITY_LINE is always slot 0 so identity survives
    # third-party endpoints that compress long system prompts.
    from xmclaw.core.persona import (
        build_system_prompt,
        ensure_default_profile,
    )
    from xmclaw.core.persona.loader import (
        ensure_bootstrap_marker,
        render_tools_section,
    )
    profile_dir = _resolve_persona_profile_dir(cfg)
    # Materialize bundled templates on first install so the user can
    # actually edit them (otherwise they only see the prompt output, not
    # the source files). Idempotent — won't overwrite existing files.
    try:
        ensure_default_profile(profile_dir)
    except OSError:
        pass
    # Wave-27 fix-LAT4: write BOOTSTRAP.md when the install looks
    # fresh (IDENTITY.md still byte-equal to the template). The
    # ``bootstrap_prefix`` in the system prompt then nudges the agent
    # to interview the user and fill IDENTITY/USER on the next turn.
    # ``ensure_bootstrap_marker`` short-circuits when IDENTITY has been
    # edited or BOOTSTRAP.md is already pending — safe to call every
    # boot.
    try:
        bs_path = ensure_bootstrap_marker(profile_dir)
        if bs_path is not None:
            import logging as _lg
            _lg.getLogger(__name__).info(
                "persona.bootstrap_marker_written path=%s", bs_path,
            )
    except Exception:  # noqa: BLE001
        pass
    workspace_root: Path | None = None
    ws_section = cfg.get("workspace") if isinstance(cfg, Mapping) else None
    if isinstance(ws_section, Mapping):
        ws_path = ws_section.get("path")
        if isinstance(ws_path, str) and ws_path.strip():
            workspace_root = Path(ws_path).expanduser()
    backend_label = _resolve_backend_label(cfg)
    # Cross-session memory: B-26 builds a MemoryManager with two
    # providers — the BuiltinFileMemoryProvider (always-on, wraps
    # MEMORY.md / USER.md) plus an external SqliteVecMemory when one
    # builds successfully. Hermes-style: only ONE external provider
    # at a time, builtin is non-removable. Both are best-effort: if
    # init fails the agent stays usable without long-term recall.
    from xmclaw.providers.memory.manager import MemoryManager
    from xmclaw.providers.memory.builtin_file import BuiltinFileMemoryProvider

    # Jarvisification: optional MemoryGraph for relational memory.
    _cognition_cfg = (cfg or {}).get("cognition") or {}
    _graph = None
    if _cognition_cfg.get("enabled", True):
        try:
            from xmclaw.cognition.memory_graph import MemoryGraph
            _graph = MemoryGraph(bus=bus)
        except Exception:  # noqa: BLE001
            pass

    memory_manager = MemoryManager(bus=bus, graph=_graph)
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
    # Sprint 3 #5 follow-up: retain a reference to the SqliteVecMemory
    # store so build_strategy_bank_from_config can wire ReasoningBank
    # against the SAME underlying vec store the agent uses for memory.
    # Other provider choices (hindsight / supermemory / mem0 / none)
    # leave ``vec_memory`` as None and the strategy bank stays disabled.
    vec_memory: SqliteVecMemory | None = None
    if provider_choice == "sqlite_vec":
        try:
            external = build_memory_from_config(cfg, bus=bus)
            if external is not None:
                memory_manager.add_provider(external)
                vec_memory = external
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
            else:
                # Unwrap retry-aware and any future wrappers that expose
                # an inner provider (e.g. ErrorAwareRetryProvider).
                inner = getattr(p, "_inner", None)
                if inner is not None:
                    yield from _walk(inner)

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

    # Re-compute tool specs + system prompt AFTER memory wiring so
    # dynamically-gated tools (e.g. memory_search) appear in the
    # system prompt sent to the LLM.  Prior to this fix tool_specs
    # were snapshotted before set_memory_manager() ran.
    tool_specs = tools.list_tools() if tools is not None else []
    try:
        render_tools_section(profile_dir, tool_specs)
    except Exception:  # noqa: BLE001
        pass
    system_prompt = build_system_prompt(
        profile_dir=profile_dir,
        workspace_dir=workspace_root,
        tool_names=[s.name for s in tool_specs],
        backend_label=backend_label,
    )

    # Wave-27 fix-LAT: ``evolution.compression.token_cap`` and the
    # post-turn msg/token gates it fed have been retired. Compression
    # now runs pre-LLM via ContextCompressor (ctx-window aware,
    # 85%-of-budget threshold) — see ``cognition.context_compression``
    # config section for its knobs (threshold_percent, protect_last_n,
    # protect_last_ratio, ...). The old knob is silently ignored.

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

    # B-112: post-sampling hooks framework. Default registry ships with
    # ExtractMemoriesHook (gated by config). New hooks can be added by
    # extending build_default_registry().
    try:
        from xmclaw.daemon.post_sampling_hooks import build_default_registry
        _hook_registry = build_default_registry()
    except Exception:  # noqa: BLE001
        _hook_registry = None

    # B-189 / Wave-27 fix-13: per-LLM-call wall-clock timeout.
    # Default bumped 120 → 300 (2026-05-15) because vision-heavy
    # turns (browser_screenshot accumulating across hops) routinely
    # spent 150-250s on a single Kimi K2.6 / MiniMax M2 call.
    # Override via ``llm.timeout_s`` in config.json — set lower for
    # fast local Ollama, higher for slow vision-heavy providers.
    _llm_timeout_s = float(
        (cfg.get("llm") or {}).get("timeout_s", 300.0)
    )

    # Wave-27 fix-17 (2026-05-16): per-TOOL-call wall-clock cap.
    # Stops a hung Playwright wait / unresponsive MCP server from
    # blocking the agent loop forever (user saw browser_click stuck
    # in "running" state with no recovery). Default 180s — generous
    # for slow page loads + cold subprocess starts, bounded so no
    # single tool can stall the turn. Override via
    # ``tools.invoke_timeout_s`` in daemon config.
    _tool_invoke_timeout_s = float(
        (cfg.get("tools") or {}).get("invoke_timeout_s", 180.0)
    )

    # B-312: daemon-level CostTracker injection. anti-req #6 calls
    # for a hard cap on token cost; pre-B-312 the AgentLoop accepted
    # ``cost_tracker`` but factory never instantiated one, leaving
    # CLI-driven instantiation (which never happens). Now: read
    # ``cfg.cost.budget_usd`` (default 0 = unlimited, matches legacy
    # behaviour) and build a daemon-scoped tracker the agent_loop
    # checks pre-LLM-call. BudgetExceeded → ANTI_REQ_VIOLATION event,
    # AgentLoop returns a friendly error to the user.
    _cost_tracker = None
    try:
        from xmclaw.utils.cost import CostTracker
        _cost_cfg = (cfg.get("cost") or {})
        _budget_usd = float(_cost_cfg.get("budget_usd", 0.0))
        if _budget_usd > 0 or _cost_cfg.get("track", False):
            _cost_tracker = CostTracker(budget_usd=_budget_usd)
    except Exception:  # noqa: BLE001 — never block boot on cost config
        _cost_tracker = None

    # Jarvisification: use provided cognitive_state or build a fresh one
    # when enabled. MultiAgentManager passes a shared instance so all
    # sub-agents operate on the same cognitive substrate.
    _cognitive_state = cognitive_state
    if _cognitive_state is None and _cognition_cfg.get("enabled", True):
        try:
            from xmclaw.cognition.state import CognitiveState
            _cognitive_state = CognitiveState()
        except Exception:  # noqa: BLE001
            pass

    # Sprint 3 #5 follow-up: optionally build the StrategyBank against
    # the daemon's SqliteVecMemory + embedder. Off by default (gated on
    # ``evolution.reasoning_bank.enabled``); silently no-op when the
    # vec store or embedder is missing — agent_loop tolerates None.
    strategy_bank = build_strategy_bank_from_config(
        cfg, memory=vec_memory, embedder=agent_embedder,
    )

    # 2026-05-10 ("agent 自己用记忆"): wire the UnifiedMemorySystem +
    # MemoryExtractor so the agent_loop's Phase A (auto-recall on turn
    # start) and Phase B (auto-put on turn end) actually run. Pre-this
    # wiring, both the unified system and its UI tab existed but the
    # agent never called either — user feedback "我的目的是给他自己用，
    # 不是光给我用" surfaced the gap.
    #
    # Default ON (mirrors Phase 6 cognition default): users with a
    # SqliteVec + MemoryGraph backend (the standard config) get the
    # auto-memory pipeline for free. Disable via:
    #   ``cfg["memory"]["unified_recall"]["enabled"] = false``
    _mem_section = (cfg or {}).get("memory") or {}
    _unified_cfg = (_mem_section.get("unified_recall") or {})
    _unified_enabled = _unified_cfg.get("enabled", True)
    _unified_top_k = max(1, int(_unified_cfg.get("top_k", 5)))
    _unified_memory = None
    _memory_extractor = None
    if _unified_enabled and (vec_memory is not None or _graph is not None):
        try:
            from xmclaw.memory import (
                MemoryExtractor as _MemoryExtractor,
                UnifiedMemorySystem as _UnifiedSystem,
            )
            _unified_memory = _UnifiedSystem(
                memory_manager=memory_manager,
                memory_graph=_graph,
                embedder=agent_embedder,
            )
            # Phase B extractor — gated by the same config block but
            # depends on a working LLM (which we always have at this
            # point — `llm` is guaranteed non-None in factory).
            _memory_extractor = _MemoryExtractor(llm=llm)
        except Exception:  # noqa: BLE001
            # Best-effort wire-up: if the unified system fails to
            # construct, fall back to legacy memory_ctx_block path
            # rather than crashing daemon boot.
            _unified_memory = None
            _memory_extractor = None

    agent_loop = AgentLoop(
        llm=llm, bus=bus, tools=tools,
        system_prompt=system_prompt,
        max_hops=max_hops,
        agent_id=cfg.get("agent_id", "agent"),
        prompt_injection_policy=policy,
        session_store=session_store,
        llm_registry=registry,
        memory=memory_arg,
        embedder=agent_embedder,
        relevant_files_picker_enabled=_picker_enabled,
        relevant_files_picker_k=_picker_k,
        relevant_files_max_chars=_picker_max_chars,
        cfg=cfg,
        post_sampling_registry=_hook_registry,
        llm_timeout_s=_llm_timeout_s,
        cost_tracker=_cost_tracker,
        cognitive_state=_cognitive_state,
        strategy_bank=strategy_bank,
        unified_memory=_unified_memory,
        unified_recall_top_k=_unified_top_k,
        memory_extractor=_memory_extractor,
        perception_bus=perception_bus,
        strict_freeze=bool(
            (cfg or {}).get("agent", {}).get("strict_freeze", False)
        ),
    )

    # Wave-27 fix-17: apply the configured tool wall-clock onto the
    # constructed AgentLoop instance — hop_loop._invoke_single_tool
    # reads it via ``getattr(self, "_tool_invoke_timeout_s", 180.0)``.
    try:
        agent_loop._tool_invoke_timeout_s = max(
            5.0, _tool_invoke_timeout_s,
        )
    except Exception:  # noqa: BLE001
        pass

    # 2026-05-12 Batch C.2: StepValidator — opt-in per-step
    # "did this advance the goal" auditor. Off by default to keep
    # baseline cost unchanged; flipped on when the agent is doing
    # high-stakes / long-chain work via tools.step_validator.enabled.
    try:
        tools_cfg = cfg.get("tools", {}) if isinstance(cfg, Mapping) else {}
        sv_cfg = tools_cfg.get("step_validator", {}) if isinstance(tools_cfg, Mapping) else {}
        if isinstance(sv_cfg, Mapping) and sv_cfg.get("enabled", False):
            from xmclaw.cognition.step_validator import StepValidator
            agent_loop._step_validator = StepValidator(
                llm=llm,
                timeout_s=float(sv_cfg.get("timeout_s", 4.0)),
                max_result_chars=int(sv_cfg.get("max_result_chars", 800)),
                enabled=True,
            )
    except Exception:  # noqa: BLE001
        pass

    return agent_loop


# ── Sprint 3 #5 follow-up: evolution-loop wiring ────────────────────
#
# These factories thread the Sprint-3 ReasoningBank components (Strategy
# Bank, Strategy Distiller, Reflective Mutator) into the daemon. Today
# only ``build_strategy_bank_from_config`` is consumed by
# ``build_agent_from_config`` — the agent_loop already knows how to
# call ``bank.retrieve(...)`` per turn (Sprint 3 #6). The Distiller
# and Mutator builders are exposed here so the upcoming SleepWorker /
# scheduler-tick ticket can pick them up without re-deriving the tier
# classification logic.
#
# All three functions are *additive*: returning ``None`` means the
# corresponding feature stays off. Callers treat None as "no-op", so
# a config without ``evolution.reasoning_bank.enabled`` keeps the
# pre-Sprint-3 runtime behaviour byte-for-byte.


def build_strategy_bank_from_config(
    cfg: dict[str, Any],
    *,
    memory: SqliteVecMemory | None,
    embedder: Any,
) -> "StrategyBank | None":
    """Build a StrategyBank when ``evolution.reasoning_bank.enabled`` is
    true AND a vec store + embedder are wired. Returns None when off,
    when memory store is missing, or when embedder is missing — caller
    treats None as "no strategy bank, agent_loop runs without it"."""
    # Lazy-import core to keep top-of-file imports stable.
    from xmclaw.core.journal.strategy_bank import StrategyBank
    rb = ((cfg.get("evolution") or {}).get("reasoning_bank")) or {}
    if not rb.get("enabled"):
        return None
    if memory is None or embedder is None:
        return None
    return StrategyBank(memory, embedder)


def build_strategy_distiller_from_config(
    cfg: dict[str, Any],
    *,
    llm: LLMProvider | None,
) -> "StrategyDistiller | None":
    """Build a StrategyDistiller when ``evolution.reasoning_bank.enabled``
    is true AND an LLM is configured. The distiller's tier is computed
    from ``llm.model`` via classify_model_tier. Returns None when off
    or when no LLM is wired (echo mode)."""
    from xmclaw.core.journal.strategy_distiller import StrategyDistiller
    from xmclaw.providers.llm._provider_profiles import classify_model_tier
    rb = ((cfg.get("evolution") or {}).get("reasoning_bank")) or {}
    if not rb.get("enabled") or llm is None:
        return None
    model_id = getattr(llm, "model", None) or ""
    tier = classify_model_tier(model_id)
    max_s = int(rb.get("max_strategies") or 7)
    return StrategyDistiller(llm, max_strategies=max_s, evolution_tier=tier)


def build_reflective_mutator_from_config(
    cfg: dict[str, Any],
    *,
    llm: LLMProvider | None,
) -> "ReflectiveMutator | None":
    """Build a ReflectiveMutator when ``evolution.reflective_mutator.enabled``
    is true AND an LLM is configured. Tier-classified via the LLM's
    ``.model`` attribute."""
    from xmclaw.core.evolution.reflective_mutator import ReflectiveMutator
    from xmclaw.providers.llm._provider_profiles import classify_model_tier
    rm = ((cfg.get("evolution") or {}).get("reflective_mutator")) or {}
    if not rm.get("enabled") or llm is None:
        return None
    model_id = getattr(llm, "model", None) or ""
    tier = classify_model_tier(model_id)
    max_per = int(rm.get("max_per_skill") or 5)
    return ReflectiveMutator(llm, max_per_skill=max_per, evolution_tier=tier)
