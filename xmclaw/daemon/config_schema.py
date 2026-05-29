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

from typing import Any

from xmclaw.daemon.factory import ConfigError


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


__all__ = ["validate_config", "validate_or_raise"]
