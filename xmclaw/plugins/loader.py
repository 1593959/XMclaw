"""Plugin loader — dynamic discovery of third-party plugins via entry points.

Epic #2 Phase 2. Complements the frozen :mod:`xmclaw.plugin_sdk` surface
with the machinery to actually *find* and *load* plugins at runtime.

Discovery group: ``xmclaw.plugins``

A third-party package declares an entry point like::

    [project.entry-points."xmclaw.plugins"]
    my_tool = "my_package.xmclaw_plugin:MyToolProvider"

The value must resolve to one of:

  * A subclass of :class:`xmclaw.plugin_sdk.ToolProvider`
  * A subclass of :class:`xmclaw.plugin_sdk.Skill`
  * A subclass of :class:`xmclaw.plugin_sdk.ChannelAdapter`
  * A factory callable (zero args) returning an instance of any of the above.

Load failures are caught and logged — a broken plugin must not crash the
daemon boot sequence.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    """One successfully-loaded plugin."""

    name: str
    entry_point_value: str
    kind: str  # "tool" | "skill" | "channel" | "unknown"
    instance: Any


@dataclass
class DiscoveryResult:
    """Outcome of a full discovery sweep."""

    tools: list[LoadedPlugin] = field(default_factory=list)
    skills: list[LoadedPlugin] = field(default_factory=list)
    channels: list[LoadedPlugin] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def discover_plugins(
    *,
    group: str = "xmclaw.plugins",
    fail_fast: bool = False,
) -> DiscoveryResult:
    """Scan entry points and load every plugin we can resolve.

    Args:
        group: entry-point group name. Default ``xmclaw.plugins``.
        fail_fast: when True, the first import/validation failure is
            re-raised instead of captured in ``result.errors``. Used
            by tests; daemon boot keeps the default ``False``.

    Returns:
        A :class:`DiscoveryResult` with successfully-loaded plugins
        bucketed by kind and any errors encountered.
    """
    result = DiscoveryResult()

    try:
        from importlib.metadata import entry_points
    except ImportError:
        result.errors.append("importlib.metadata not available (Python < 3.8)")
        return result

    try:
        eps = entry_points(group=group)
    except TypeError:
        # Python 3.9 fallback.
        eps = entry_points().get(group, [])

    # Deferred imports — plugin_sdk is the only xmclaw.* import we touch.
    _abc_map = _build_abc_map()

    for ep in eps:
        try:
            obj = ep.load()
        except Exception as exc:  # noqa: BLE001
            msg = f"plugin:{ep.name}: import failed: {type(exc).__name__}: {exc}"
            if fail_fast:
                raise RuntimeError(msg) from exc
            result.errors.append(msg)
            continue

        instance, kind = _resolve_instance(obj, _abc_map)
        if instance is None:
            msg = (
                f"plugin:{ep.name}: entry point did not resolve to a "
                f"recognised plugin type (got {type(obj).__name__})"
            )
            if fail_fast:
                raise TypeError(msg)
            result.errors.append(msg)
            continue

        result.tools.append(LoadedPlugin(
            name=ep.name,
            entry_point_value=ep.value,
            kind=kind,
            instance=instance,
        )) if kind == "tool" else None
        result.skills.append(LoadedPlugin(
            name=ep.name,
            entry_point_value=ep.value,
            kind=kind,
            instance=instance,
        )) if kind == "skill" else None
        result.channels.append(LoadedPlugin(
            name=ep.name,
            entry_point_value=ep.value,
            kind=kind,
            instance=instance,
        )) if kind == "channel" else None

    return result


def _build_abc_map() -> dict[str, type]:
    """Return a mapping of kind -> ABC class."""
    from xmclaw.plugin_sdk import (
        ChannelAdapter,
        Skill,
        ToolProvider,
    )
    return {
        "tool": ToolProvider,
        "skill": Skill,
        "channel": ChannelAdapter,
    }


def _resolve_instance(
    obj: Any, abc_map: dict[str, type],
) -> tuple[Any, str] | tuple[None, str]:
    """Turn an entry-point object into a plugin instance + kind.

    Resolution order:
      1. If ``obj`` is an instance of a recognised ABC → use directly.
      2. If ``obj`` is a subclass of a recognised ABC → instantiate.
      3. If ``obj`` is callable → call it and recurse on the result.
      4. Otherwise → unrecognised.
    """
    # 1. Already an instance?
    for kind, abc in abc_map.items():
        if isinstance(obj, abc):
            return obj, kind

    # 2. Class that inherits an ABC?
    if isinstance(obj, type):
        for kind, abc in abc_map.items():
            if issubclass(obj, abc):
                try:
                    return obj(), kind
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "plugin.instantiate_failed kind=%s cls=%s err=%s",
                        kind, obj.__name__, exc,
                    )
                    return None, ""

    # 3. Factory callable?
    if callable(obj) and not isinstance(obj, type):
        try:
            candidate = obj()
        except Exception as exc:  # noqa: BLE001
            _log.warning("plugin.factory_failed err=%s", exc)
            return None, ""
        return _resolve_instance(candidate, abc_map)

    return None, ""
