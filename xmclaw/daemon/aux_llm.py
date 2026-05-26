"""Auxiliary-LLM resolver.

Background
==========

The agent's main LLM is whatever the user picked in the model
picker (often a flagship: deepseek-v4-pro / kimi-k2.6 / claude-
opus). Every user turn fires several SECONDARY LLM calls
besides the main hop:

* ``LLMFactExtractor`` — extract identity / preference / decision
  facts from each user message (async background).
* ``PlanFirstGate`` — decompose multi-step user goals before the
  main hop.
* ``reflection_materializer`` — turn post-hoc reflection prompts
  into stored lessons (background).
* ``step_validator`` — judge per-tool advancement (opt-in).

These auxiliary tasks are commodity work — short prompts, simple
classification, no agency. Running them on the same flagship as
the main hop wastes money: user complaint '5 毛 a sentence' was
partly because each turn fired 3-4 aux calls at flagship rates.

Strategy
--------

If the user has registered a ``fast`` tier model in their
``LLMRegistry`` (via ``daemon/config.json`` ``llm.profiles`` or
the multi-model UI), route every aux task to it. Otherwise fall
back to the main LLM — back-compat with single-model installs.

Resolution order (per call):
  1. ``LLMRegistry.pick_by_tier("fast")`` if registry available
  2. ``LLMRegistry.pick_by_tier("balanced")`` as a middle option
  3. The supplied ``main_llm`` (last resort)

The helper caches the resolved LLM on the registry so we don't
re-walk the profile list on every aux call.
"""
from __future__ import annotations

from typing import Any


_AUX_LLM_CACHE_ATTR = "_xmc_resolved_aux_llm"


def resolve_aux_llm(
    registry: Any | None,
    main_llm: Any | None,
) -> Any | None:
    """Pick a cheap LLM for auxiliary tasks.

    Returns the resolved LLM provider. None when both ``registry``
    and ``main_llm`` are None (caller must guard).

    ``registry`` is the daemon's ``LLMRegistry``. When it has a
    profile tagged ``tier="fast"`` we use that. Otherwise we walk
    ``"balanced"`` then fall back to ``main_llm``.
    """
    if registry is None:
        return main_llm
    cached = getattr(registry, _AUX_LLM_CACHE_ATTR, None)
    if cached is not None:
        return cached
    resolved: Any | None = None
    try:
        # pick_by_tier already understands fallback_chain, but we
        # want a SPECIFIC preference order: fast > balanced > main.
        # Asking for "fast" with fallback("balanced",) returns
        # balanced when fast is missing, which is what we want.
        profile = registry.pick_by_tier("fast", fallback_chain=("balanced",))
        if profile is not None:
            resolved = getattr(profile, "llm", None)
    except Exception:  # noqa: BLE001
        resolved = None
    if resolved is None:
        resolved = main_llm
    # Cache on the registry so subsequent aux calls don't re-walk.
    try:
        setattr(registry, _AUX_LLM_CACHE_ATTR, resolved)
    except Exception:  # noqa: BLE001
        pass
    return resolved


__all__ = ["resolve_aux_llm"]
