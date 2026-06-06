"""Config schema validation — fail fast on bad ``daemon/config.json``.

REMEDIATION_PLAN_2026 P1-4. Pre-fix, an invalid value (negative port,
``autonomy_level=150``, ``evolution.auto_apply="yes"`` as a string)
crashed deep inside a feature module with a cryptic
``AttributeError`` or silent type coercion. Now we catch those at
``load_config`` time and raise a single ``ConfigError`` pointing at
the bad path so the user fixes it in one round-trip.

Design notes:

* **No external dep.** The original plan called for ``jsonschema``,
  but pulling a 100K-LOC dependency for ~20 known-bad shapes is
  overkill. A hand-rolled validator is ~80 lines and produces
  better error messages anyway (we can point at the exact path
  and explain WHY a value is wrong).
* **Each rule independent.** Validation collects every failure
  before raising, so the user sees ALL problems in their config
  on one run instead of fix-restart-find-next loop.
* **Coverage choice.** We validate (1) numeric ranges that, when
  wrong, produce silent misbehaviour (autonomy out of [0, 100]
  routes through the wrong code path), (2) types where the
  read site assumes a specific shape (port must int-coerce,
  retention dict layout), (3) enumerated string fields (action
  names, scope names). We intentionally don't try to be
  exhaustive — that's what runtime ``ConfigError`` raises in the
  builders are for.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

from xmclaw.daemon.factory import ConfigError


# ── model whitelist ──────────────────────────────────────────────
_COMMON_MODELS: set[str] = {
    "gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4.1", "gpt-4.5", "gpt-4",
    "claude-3-5-sonnet", "claude-3-5-haiku", "claude-opus-4", "claude-sonnet-4",
    "claude-haiku-4-5", "claude-3-7-sonnet", "claude-3-5-sonnet-20241022",
    "kimi-k2", "kimi-k2.6", "kimi-k2.5", "kimi-k1.5",
    "qwen3", "qwen-max", "qwen-turbo", "qwen-plus", "qwen3-embedding",
    "deepseek-v3", "deepseek-r1", "deepseek-chat", "deepseek-coder",
    "llama-3-8b", "llama-3-70b", "llama-3-405b", "llama3-8b", "llama3-70b",
    "llama3-405b", "gemini-1.5-pro", "gemini-1.5-flash", "gemini-2.0-flash",
    "o1", "o3", "o1-mini", "o3-mini", "gpt-3.5-turbo", "claude-3-opus",
    "claude-3-sonnet", "claude-3-haiku",
    # OpenRouter / provider-prefixed forms
    "anthropic/claude-sonnet-4", "anthropic/claude-haiku-4-5",
    "openai/gpt-4o", "openai/gpt-4o-mini",
    "openrouter/anthropic/claude-sonnet-4",
}

_LOCAL_SMALL_PATTERNS: tuple[str, ...] = (
    "ollama/llama-3-8b", "ollama/llama3-8b", "ollama/phi", "ollama/gemma-2b",
    "ollama/gemma-7b", "ollama/tinyllama", "ollama/qwen-7b", "ollama/qwen2-7b",
    "ollama/mistral-7b", "ollama/deepseek-v2-lite",
)


def _is_valid_url(url: str) -> bool:
    """Return True if *url* has a http/https scheme and a non-empty netloc."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:  # noqa: BLE001
        return False


def _is_valid_host(host: str) -> bool:
    """Return True for valid IPv4/IPv6 addresses or hostnames."""
    if not host or not isinstance(host, str):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    # hostname / FQDN (allows localhost, single labels, and dotted domains)
    if re.match(
        r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$",
        host,
    ):
        return True
    return False


def _is_valid_path_or_uri(value: str) -> bool:
    """Lenient check for a LanceDB URI / filesystem path."""
    if not value or not isinstance(value, str):
        return False
    # If it looks like a URL, validate it as one
    if "://" in value:
        return _is_valid_url(value)
    # Otherwise accept any non-empty string as a path
    return bool(value.strip())


def _is_valid_model_name(model: str) -> bool:
    """Whitelist + format check for LLM model names."""
    if not model or not isinstance(model, str):
        return False
    model_lower = model.lower().strip()
    if model_lower in _COMMON_MODELS:
        return True
    # {provider}/{model} format (e.g. anthropic/claude-3-5-sonnet)
    if re.match(r"^[a-zA-Z0-9_\-]+/[a-zA-Z0-9_\-\.]+$", model_lower):
        return True
    # Ollama format
    if re.match(r"^ollama/[a-zA-Z0-9_\-\.:]+$", model_lower):
        return True
    return False


def _is_local_small_model(model: str) -> bool:
    """Heuristic: is this a local small model that evolution should avoid?"""
    if not model or not isinstance(model, str):
        return False
    model_lower = model.lower().strip()
    if any(model_lower == p for p in _LOCAL_SMALL_PATTERNS):
        return True
    if model_lower.startswith("ollama/"):
        small_indicators = (
            "8b", "7b", "2b", "3b", "4b", "tiny", "mini", "small", "phi", "gemma"
        )
        if any(ind in model_lower for ind in small_indicators):
            return True
    return False


def _get_nested(cfg: dict[str, Any], *keys: str) -> Any:
    """Safely traverse nested dicts; return None on any missing key."""
    cursor: Any = cfg
    for k in keys:
        if isinstance(cursor, dict):
            cursor = cursor.get(k)
        else:
            return None
    return cursor


def validate_config(cfg: dict[str, Any]) -> list[str]:
    """Return a list of human-readable error strings. Empty list means
    config is valid (as far as the static schema cares).

    Callers typically do::

        errors = validate_config(cfg)
        if errors:
            raise ConfigError(
                "config schema validation failed:\\n  - "
                + "\\n  - ".join(errors)
            )
    """
    errors: list[str] = []

    # ── llm ──────────────────────────────────────────────────────
    llm = cfg.get("llm")
    if llm is not None and not isinstance(llm, dict):
        errors.append(f"llm: expected object, got {type(llm).__name__}")
    elif isinstance(llm, dict):
        profiles = llm.get("profiles")
        if profiles is not None:
            if not isinstance(profiles, list):
                errors.append(
                    f"llm.profiles: expected list, got "
                    f"{type(profiles).__name__}"
                )
            else:
                seen_ids: set[str] = set()
                for i, prof in enumerate(profiles):
                    if not isinstance(prof, dict):
                        errors.append(
                            f"llm.profiles[{i}]: expected object, got "
                            f"{type(prof).__name__}"
                        )
                        continue
                    pid = prof.get("id")
                    if not isinstance(pid, str) or not pid.strip():
                        errors.append(
                            f"llm.profiles[{i}].id: required non-empty string"
                        )
                    elif pid in seen_ids:
                        errors.append(
                            f"llm.profiles[{i}].id: duplicate id {pid!r}"
                        )
                    else:
                        seen_ids.add(pid)
                    sv = prof.get("supports_vision")
                    if sv is not None and not isinstance(sv, bool):
                        errors.append(
                            f"llm.profiles[{i}].supports_vision: "
                            f"expected bool, got {type(sv).__name__}"
                        )

    # ── gateway ──────────────────────────────────────────────────
    gw = cfg.get("gateway")
    if isinstance(gw, dict):
        port = gw.get("port")
        if port is not None:
            if not isinstance(port, int) or isinstance(port, bool):
                errors.append(
                    f"gateway.port: expected int, got "
                    f"{type(port).__name__}"
                )
            elif not (1 <= port <= 65535):
                errors.append(
                    f"gateway.port: must be in [1, 65535], got {port}"
                )
        host = gw.get("host")
        if host is not None and not isinstance(host, str):
            errors.append(
                f"gateway.host: expected string, got {type(host).__name__}"
            )

    # ── cognition ────────────────────────────────────────────────
    cog = cfg.get("cognition")
    if isinstance(cog, dict):
        # continuous_loop.autonomy_level ∈ [0, 100]
        cl = cog.get("continuous_loop")
        if isinstance(cl, dict):
            aut = cl.get("autonomy_level")
            if aut is not None:
                if isinstance(aut, bool) or not isinstance(aut, int):
                    errors.append(
                        f"cognition.continuous_loop.autonomy_level: "
                        f"expected int, got {type(aut).__name__}"
                    )
                elif not (0 <= aut <= 100):
                    errors.append(
                        f"cognition.continuous_loop.autonomy_level: "
                        f"must be in [0, 100], got {aut}"
                    )
            hb = cl.get("heartbeat_hz")
            if hb is not None and not isinstance(hb, (int, float)):
                errors.append(
                    f"cognition.continuous_loop.heartbeat_hz: "
                    f"expected number, got {type(hb).__name__}"
                )
            elif isinstance(hb, (int, float)) and hb <= 0:
                errors.append(
                    f"cognition.continuous_loop.heartbeat_hz: "
                    f"must be > 0, got {hb}"
                )

        # auto_recall block (v3 phase 2)
        ar = cog.get("auto_recall")
        if isinstance(ar, dict):
            for fld in ("enabled", "use_hybrid"):
                v = ar.get(fld)
                if v is not None and not isinstance(v, bool):
                    errors.append(
                        f"cognition.auto_recall.{fld}: expected bool, "
                        f"got {type(v).__name__}"
                    )
            ts = ar.get("timeout_s")
            if ts is not None:
                if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                    errors.append(
                        f"cognition.auto_recall.timeout_s: expected number, "
                        f"got {type(ts).__name__}"
                    )
                elif ts <= 0:
                    errors.append(
                        f"cognition.auto_recall.timeout_s: must be > 0, "
                        f"got {ts}"
                    )
            ms = ar.get("min_similarity")
            if ms is not None:
                if isinstance(ms, bool) or not isinstance(ms, (int, float)):
                    errors.append(
                        f"cognition.auto_recall.min_similarity: "
                        f"expected number, got {type(ms).__name__}"
                    )
                elif not (0.0 <= ms <= 1.0):
                    errors.append(
                        f"cognition.auto_recall.min_similarity: "
                        f"must be in [0.0, 1.0], got {ms}"
                    )

        # memory_v2.retention shape
        mv2 = cog.get("memory_v2")
        if isinstance(mv2, dict):
            ret = mv2.get("retention")
            if isinstance(ret, dict):
                si = ret.get("sweep_interval_s")
                if si is not None:
                    if isinstance(si, bool) or not isinstance(si, (int, float)):
                        errors.append(
                            f"cognition.memory_v2.retention.sweep_interval_s: "
                            f"expected number, got {type(si).__name__}"
                        )
                    elif si < 0:
                        errors.append(
                            f"cognition.memory_v2.retention.sweep_interval_s: "
                            f"must be >= 0 (0 disables), got {si}"
                        )
                dens = ret.get("dedup_every_n_sweeps")
                if dens is not None:
                    if isinstance(dens, bool) or not isinstance(dens, int):
                        errors.append(
                            f"cognition.memory_v2.retention.dedup_every_n_sweeps: "
                            f"expected int, got {type(dens).__name__}"
                        )
                    elif dens < 0:
                        errors.append(
                            f"cognition.memory_v2.retention.dedup_every_n_sweeps: "
                            f"must be >= 0, got {dens}"
                        )
                ldens = ret.get("llm_dedup_every_n_sweeps")
                if ldens is not None:
                    if isinstance(ldens, bool) or not isinstance(ldens, int):
                        errors.append(
                            f"cognition.memory_v2.retention."
                            f"llm_dedup_every_n_sweeps: expected int, "
                            f"got {type(ldens).__name__}"
                        )
                    elif ldens < 0:
                        errors.append(
                            f"cognition.memory_v2.retention."
                            f"llm_dedup_every_n_sweeps: must be >= 0, "
                            f"got {ldens}"
                        )
                dsc = ret.get("dedup_scopes")
                if dsc is not None:
                    if not isinstance(dsc, list) or not all(
                        isinstance(s, str) for s in dsc
                    ):
                        errors.append(
                            f"cognition.memory_v2.retention.dedup_scopes: "
                            f"expected list of strings, got "
                            f"{type(dsc).__name__}"
                        )

            # memory_v2.curator shape (Phase 8 — MemoryCurator)
            cur = mv2.get("curator")
            if isinstance(cur, dict):
                for fld in (
                    "enabled", "announce", "do_dedup", "do_prune",
                    "do_contradict", "do_crystallize",
                ):
                    v = cur.get(fld)
                    if v is not None and not isinstance(v, bool):
                        errors.append(
                            f"cognition.memory_v2.curator.{fld}: "
                            f"expected bool, got {type(v).__name__}"
                        )
                for fld in (
                    "interval_s", "check_interval_s", "warmup_s",
                    "time_budget_s",
                ):
                    v = cur.get(fld)
                    if v is not None:
                        if isinstance(v, bool) or not isinstance(
                            v, (int, float)
                        ):
                            errors.append(
                                f"cognition.memory_v2.curator.{fld}: "
                                f"expected number, got {type(v).__name__}"
                            )
                        elif v <= 0:
                            errors.append(
                                f"cognition.memory_v2.curator.{fld}: "
                                f"must be > 0, got {v}"
                            )
                csc = cur.get("scopes")
                if csc is not None:
                    if not isinstance(csc, list) or not all(
                        isinstance(s, str) for s in csc
                    ):
                        errors.append(
                            f"cognition.memory_v2.curator.scopes: "
                            f"expected list of strings, got "
                            f"{type(csc).__name__}"
                        )

            # memory_v2.write_decision shape (Phase 8 ⑨ — Mem0 route)
            wd = mv2.get("write_decision")
            if isinstance(wd, dict):
                en = wd.get("enabled")
                if en is not None and not isinstance(en, bool):
                    errors.append(
                        f"cognition.memory_v2.write_decision.enabled: "
                        f"expected bool, got {type(en).__name__}"
                    )

    # ── evolution ────────────────────────────────────────────────
    ev = cfg.get("evolution")
    if isinstance(ev, dict):
        for fld in ("enabled", "auto_apply"):
            v = ev.get(fld)
            if v is not None and not isinstance(v, bool):
                errors.append(
                    f"evolution.{fld}: expected bool, got "
                    f"{type(v).__name__}"
                )

    # ── skills.semantic_discovery (§⑫ autonomous invocation) ─────
    sk = cfg.get("skills")
    if isinstance(sk, dict):
        sd = sk.get("semantic_discovery")
        if isinstance(sd, dict):
            en = sd.get("enabled")
            if en is not None and not isinstance(en, bool):
                errors.append(
                    f"skills.semantic_discovery.enabled: expected bool, "
                    f"got {type(en).__name__}"
                )
            fl = sd.get("floor")
            if fl is not None:
                if isinstance(fl, bool) or not isinstance(fl, (int, float)):
                    errors.append(
                        f"skills.semantic_discovery.floor: expected number, "
                        f"got {type(fl).__name__}"
                    )
                elif not (0.0 <= fl <= 1.0):
                    errors.append(
                        f"skills.semantic_discovery.floor: must be in "
                        f"[0.0, 1.0], got {fl}"
                    )
        # skills.induction (§② trajectory→skill)
        ind = sk.get("induction")
        if isinstance(ind, dict):
            for fld in ("enabled", "announce"):
                v = ind.get(fld)
                if v is not None and not isinstance(v, bool):
                    errors.append(
                        f"skills.induction.{fld}: expected bool, got "
                        f"{type(v).__name__}"
                    )
            for fld in ("interval_s", "check_interval_s", "warmup_s"):
                v = ind.get(fld)
                if v is not None:
                    if isinstance(v, bool) or not isinstance(v, (int, float)):
                        errors.append(
                            f"skills.induction.{fld}: expected number, got "
                            f"{type(v).__name__}"
                        )
                    elif v <= 0:
                        errors.append(
                            f"skills.induction.{fld}: must be > 0, got {v}"
                        )
            mpp = ind.get("max_per_pass")
            if mpp is not None:
                if isinstance(mpp, bool) or not isinstance(mpp, int):
                    errors.append(
                        f"skills.induction.max_per_pass: expected int, got "
                        f"{type(mpp).__name__}"
                    )
                elif mpp < 1:
                    errors.append(
                        f"skills.induction.max_per_pass: must be >= 1, "
                        f"got {mpp}"
                    )

    return errors


def validate_or_raise(cfg: dict[str, Any]) -> None:
    """Run :func:`validate_config` and raise ``ConfigError`` on any
    failure. Convenience wrapper for the boot path."""
    problems = validate_config(cfg)
    if not problems:
        return
    raise ConfigError(
        "config schema validation failed ({} problem{}):\n  - {}".format(
            len(problems),
            "" if len(problems) == 1 else "s",
            "\n  - ".join(problems),
        )
    )


# ── extended linter ──────────────────────────────────────────────
def lint_config(cfg: dict[str, Any]) -> list[ConfigError]:
    """Extended config linter — returns ALL errors as ``ConfigError`` instances.

    Includes the existing schema checks plus new URL, model whitelist,
    numeric range, and dependency validations.  Callers (e.g. the factory
    builders) log the returned errors as warnings and continue booting
    so the daemon stays best-effort.
    """
    errors: list[ConfigError] = []

    # 1. Existing schema validations
    for err in validate_config(cfg):
        errors.append(ConfigError(err))

    # 2. URL format validation
    llm = cfg.get("llm")
    if isinstance(llm, dict):
        for provider in ("openai", "anthropic", "openrouter"):
            pcfg = llm.get(provider)
            if isinstance(pcfg, dict):
                base_url = pcfg.get("base_url")
                if isinstance(base_url, str) and base_url.strip():
                    if not _is_valid_url(base_url):
                        errors.append(ConfigError(
                            f"llm.{provider}.base_url: 无效URL格式 / invalid URL format: {base_url!r}"
                        ))
        profiles = llm.get("profiles")
        if isinstance(profiles, list):
            for i, prof in enumerate(profiles):
                if isinstance(prof, dict):
                    base_url = prof.get("base_url")
                    if isinstance(base_url, str) and base_url.strip():
                        if not _is_valid_url(base_url):
                            errors.append(ConfigError(
                                f"llm.profiles[{i}].base_url: 无效URL格式 / invalid URL format: {base_url!r}"
                            ))

    # memory.v2.lancedb_uri
    memory_v2 = _get_nested(cfg, "cognition", "memory_v2")
    if isinstance(memory_v2, dict):
        lancedb_uri = memory_v2.get("lancedb_uri")
        if isinstance(lancedb_uri, str) and lancedb_uri.strip():
            if not _is_valid_path_or_uri(lancedb_uri):
                errors.append(ConfigError(
                    f"cognition.memory_v2.lancedb_uri: 无效路径或URI / invalid path or URI: {lancedb_uri!r}"
                ))
    # Also accept top-level memory.v2 if present
    memory = cfg.get("memory")
    if isinstance(memory, dict):
        mv2 = memory.get("v2")
        if isinstance(mv2, dict):
            lancedb_uri = mv2.get("lancedb_uri")
            if isinstance(lancedb_uri, str) and lancedb_uri.strip():
                if not _is_valid_path_or_uri(lancedb_uri):
                    errors.append(ConfigError(
                        f"memory.v2.lancedb_uri: 无效路径或URI / invalid path or URI: {lancedb_uri!r}"
                    ))

    # server.host / gateway.host
    for host_path, host_val in (
        ("gateway.host", _get_nested(cfg, "gateway", "host")),
        ("server.host", _get_nested(cfg, "server", "host")),
    ):
        if isinstance(host_val, str) and host_val.strip():
            if not _is_valid_host(host_val):
                errors.append(ConfigError(
                    f"{host_path}: 无效IP或域名 / invalid IP or domain: {host_val!r}"
                ))

    # 3. Model name whitelist
    if isinstance(llm, dict):
        for provider in ("openai", "anthropic", "openrouter"):
            pcfg = llm.get(provider)
            if isinstance(pcfg, dict):
                model = pcfg.get("default_model") or pcfg.get("model")
                if isinstance(model, str) and model.strip():
                    if not _is_valid_model_name(model):
                        errors.append(ConfigError(
                            f"llm.{provider}.default_model: 未知模型名称 / unknown model name: {model!r} "
                            f"(expected known model or {{provider}}/{{model}} format, or ollama/{{model_name}})"
                        ))
        profiles = llm.get("profiles")
        if isinstance(profiles, list):
            for i, prof in enumerate(profiles):
                if isinstance(prof, dict):
                    model = prof.get("model") or prof.get("default_model")
                    if isinstance(model, str) and model.strip():
                        if not _is_valid_model_name(model):
                            errors.append(ConfigError(
                                f"llm.profiles[{i}].model: 未知模型名称 / unknown model name: {model!r} "
                                f"(expected known model or {{provider}}/{{model}} format, or ollama/{{model_name}})"
                            ))

    # 4. Numeric range validation
    # llm.temperature & max_tokens
    if isinstance(llm, dict):
        for provider in ("openai", "anthropic", "openrouter"):
            pcfg = llm.get(provider)
            if isinstance(pcfg, dict):
                temp = pcfg.get("temperature")
                if temp is not None:
                    if isinstance(temp, bool) or not isinstance(temp, (int, float)):
                        errors.append(ConfigError(
                            f"llm.{provider}.temperature: 应为数字 / expected number, got {type(temp).__name__}"
                        ))
                    elif not (0.0 <= float(temp) <= 2.0):
                        errors.append(ConfigError(
                            f"llm.{provider}.temperature: 必须在[0.0, 2.0]范围内 / must be in [0.0, 2.0], got {temp}"
                        ))
                max_tok = pcfg.get("max_tokens")
                if max_tok is not None:
                    if isinstance(max_tok, bool) or not isinstance(max_tok, int):
                        errors.append(ConfigError(
                            f"llm.{provider}.max_tokens: 应为整数 / expected int, got {type(max_tok).__name__}"
                        ))
                    elif not (1 <= max_tok <= 128000):
                        errors.append(ConfigError(
                            f"llm.{provider}.max_tokens: 必须在[1, 128000]范围内 / must be in [1, 128000], got {max_tok}"
                        ))
        profiles = llm.get("profiles")
        if isinstance(profiles, list):
            for i, prof in enumerate(profiles):
                if isinstance(prof, dict):
                    temp = prof.get("temperature")
                    if temp is not None:
                        if isinstance(temp, bool) or not isinstance(temp, (int, float)):
                            errors.append(ConfigError(
                                f"llm.profiles[{i}].temperature: 应为数字 / expected number, got {type(temp).__name__}"
                            ))
                        elif not (0.0 <= float(temp) <= 2.0):
                            errors.append(ConfigError(
                                f"llm.profiles[{i}].temperature: 必须在[0.0, 2.0]范围内 / must be in [0.0, 2.0], got {temp}"
                            ))
                    max_tok = prof.get("max_tokens")
                    if max_tok is not None:
                        if isinstance(max_tok, bool) or not isinstance(max_tok, int):
                            errors.append(ConfigError(
                                f"llm.profiles[{i}].max_tokens: 应为整数 / expected int, got {type(max_tok).__name__}"
                            ))
                        elif not (1 <= max_tok <= 128000):
                            errors.append(ConfigError(
                                f"llm.profiles[{i}].max_tokens: 必须在[1, 128000]范围内 / must be in [1, 128000], got {max_tok}"
                            ))

    # agent.max_hops & agent.llm_timeout_s
    agent = cfg.get("agent")
    if isinstance(agent, dict):
        max_hops = agent.get("max_hops")
        if max_hops is not None:
            if isinstance(max_hops, bool) or not isinstance(max_hops, int):
                errors.append(ConfigError(
                    f"agent.max_hops: 应为整数 / expected int, got {type(max_hops).__name__}"
                ))
            elif not (1 <= max_hops <= 100):
                errors.append(ConfigError(
                    f"agent.max_hops: 必须在[1, 100]范围内 / must be in [1, 100], got {max_hops}"
                ))
        llm_timeout = agent.get("llm_timeout_s")
        if llm_timeout is not None:
            if isinstance(llm_timeout, bool) or not isinstance(llm_timeout, (int, float)):
                errors.append(ConfigError(
                    f"agent.llm_timeout_s: 应为数字 / expected number, got {type(llm_timeout).__name__}"
                ))
            elif not (5 <= float(llm_timeout) <= 600):
                errors.append(ConfigError(
                    f"agent.llm_timeout_s: 必须在[5, 600]范围内 / must be in [5, 600], got {llm_timeout}"
                ))

    # llm.timeout_s (factory.py reads this path)
    if isinstance(llm, dict):
        llm_timeout = llm.get("timeout_s")
        if llm_timeout is not None:
            if isinstance(llm_timeout, bool) or not isinstance(llm_timeout, (int, float)):
                errors.append(ConfigError(
                    f"llm.timeout_s: 应为数字 / expected number, got {type(llm_timeout).__name__}"
                ))
            elif not (5 <= float(llm_timeout) <= 600):
                errors.append(ConfigError(
                    f"llm.timeout_s: 必须在[5, 600]范围内 / must be in [5, 600], got {llm_timeout}"
                ))

    # tools.invoke_timeout_s
    tools = cfg.get("tools")
    if isinstance(tools, dict):
        invoke_timeout = tools.get("invoke_timeout_s")
        if invoke_timeout is not None:
            if isinstance(invoke_timeout, bool) or not isinstance(invoke_timeout, (int, float)):
                errors.append(ConfigError(
                    f"tools.invoke_timeout_s: 应为数字 / expected number, got {type(invoke_timeout).__name__}"
                ))
            elif not (1 <= float(invoke_timeout) <= 600):
                errors.append(ConfigError(
                    f"tools.invoke_timeout_s: 必须在[1, 600]范围内 / must be in [1, 600], got {invoke_timeout}"
                ))

    # memory.v2.max_entries
    if isinstance(memory_v2, dict):
        max_entries = memory_v2.get("max_entries")
        if max_entries is not None:
            if isinstance(max_entries, bool) or not isinstance(max_entries, int):
                errors.append(ConfigError(
                    f"cognition.memory_v2.max_entries: 应为整数 / expected int, got {type(max_entries).__name__}"
                ))
            elif not (100 <= max_entries <= 1000000):
                errors.append(ConfigError(
                    f"cognition.memory_v2.max_entries: 必须在[100, 1000000]范围内 / must be in [100, 1000000], got {max_entries}"
                ))
    if isinstance(memory, dict):
        mv2 = memory.get("v2")
        if isinstance(mv2, dict):
            max_entries = mv2.get("max_entries")
            if max_entries is not None:
                if isinstance(max_entries, bool) or not isinstance(max_entries, int):
                    errors.append(ConfigError(
                        f"memory.v2.max_entries: 应为整数 / expected int, got {type(max_entries).__name__}"
                    ))
                elif not (100 <= max_entries <= 1000000):
                    errors.append(ConfigError(
                        f"memory.v2.max_entries: 必须在[100, 1000000]范围内 / must be in [100, 1000000], got {max_entries}"
                    ))

    # 5. Dependency validation
    # memory.v2.enabled=true → memory.v2.lancedb_uri must exist
    if isinstance(memory_v2, dict):
        if memory_v2.get("enabled") is True:
            lancedb_uri = memory_v2.get("lancedb_uri")
            if lancedb_uri is None or (isinstance(lancedb_uri, str) and not lancedb_uri.strip()):
                errors.append(ConfigError(
                    f"cognition.memory_v2.enabled=true 但缺少 lancedb_uri / cognition.memory_v2.enabled=true but lancedb_uri is missing"
                ))
    if isinstance(memory, dict):
        mv2 = memory.get("v2")
        if isinstance(mv2, dict):
            if mv2.get("enabled") is True:
                lancedb_uri = mv2.get("lancedb_uri")
                if lancedb_uri is None or (isinstance(lancedb_uri, str) and not lancedb_uri.strip()):
                    errors.append(ConfigError(
                        f"memory.v2.enabled=true 但缺少 lancedb_uri / memory.v2.enabled=true but lancedb_uri is missing"
                    ))

    # swarm.enabled=true → swarm.max_subagents >= 2
    swarm = cfg.get("swarm")
    if isinstance(swarm, dict):
        if swarm.get("enabled") is True:
            max_sub = swarm.get("max_subagents")
            if max_sub is not None:
                if isinstance(max_sub, bool) or not isinstance(max_sub, int):
                    errors.append(ConfigError(
                        f"swarm.max_subagents: 应为整数 / expected int, got {type(max_sub).__name__}"
                    ))
                elif max_sub < 2:
                    errors.append(ConfigError(
                        f"swarm.enabled=true 时 max_subagents 必须>=2 / when swarm.enabled=true, max_subagents must be >= 2, got {max_sub}"
                    ))
            else:
                errors.append(ConfigError(
                    f"swarm.enabled=true 但缺少 max_subagents / swarm.enabled=true but max_subagents is missing"
                ))

    # evolution.enabled=true → llm.model cannot be local small model
    evolution = cfg.get("evolution")
    if isinstance(evolution, dict) and evolution.get("enabled") is True:
        active_model = None
        if isinstance(llm, dict):
            for provider in ("openai", "anthropic", "openrouter"):
                pcfg = llm.get(provider)
                if isinstance(pcfg, dict):
                    model = pcfg.get("default_model") or pcfg.get("model")
                    if isinstance(model, str) and model.strip():
                        active_model = model
                        break
            if active_model is None:
                profiles = llm.get("profiles")
                if isinstance(profiles, list):
                    for prof in profiles:
                        if isinstance(prof, dict):
                            model = prof.get("model") or prof.get("default_model")
                            if isinstance(model, str) and model.strip():
                                active_model = model
                                break
        if active_model and _is_local_small_model(active_model):
            errors.append(ConfigError(
                f"evolution.enabled=true 但 llm.model 是本地小模型({active_model}) / evolution.enabled=true but llm.model is a local small model ({active_model})"
            ))

    return errors


__all__ = ["validate_config", "validate_or_raise", "lint_config"]
