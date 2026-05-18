"""Feature flags — 5-layer resolution (env / memory / disk / remote /
default) for runtime knob control. See ``engine.py`` for the
priority story.

Public entry:
    from xmclaw.core.feature_flags import default_engine, FeatureFlag

    flag = default_engine().variant("cognition.idle_aware")
    if default_engine().is_enabled("evolution.skill_mutation"):
        ...
"""
from __future__ import annotations

from typing import Any

from xmclaw.core.feature_flags.engine import (
    FeatureFlagEngine,
    NoopRemoteProvider,
    RemoteProvider,
)
from xmclaw.core.feature_flags.flags import FeatureFlag, Variant

_DEFAULT: FeatureFlagEngine | None = None


def default_engine() -> FeatureFlagEngine:
    """Lazy module-level singleton. Constructed on first access with
    ``disk_path = data_dir()/v2/features.json`` and the noop remote.

    The daemon's lifespan can replace this with a richer instance
    (real remote provider, scoped per-agent registry) via
    :func:`set_default_engine`.
    """
    global _DEFAULT
    if _DEFAULT is None:
        from xmclaw.utils.paths import data_dir
        _DEFAULT = FeatureFlagEngine(
            disk_path=data_dir() / "v2" / "features.json",
        )
        # Auto-register the built-in flag catalogue so consumers can
        # rely on ``flag.default`` even before the lifespan wires a
        # custom registry.
        from xmclaw.core.feature_flags.registry import BUILTIN_FLAGS
        _DEFAULT.register_many(BUILTIN_FLAGS)
    return _DEFAULT


def set_default_engine(engine: FeatureFlagEngine | None) -> None:
    """Daemon-side hook. Tests use this to inject a fresh engine
    per-test (no disk persistence)."""
    global _DEFAULT
    _DEFAULT = engine


__all__ = [
    "FeatureFlag",
    "FeatureFlagEngine",
    "NoopRemoteProvider",
    "RemoteProvider",
    "Variant",
    "default_engine",
    "set_default_engine",
]
