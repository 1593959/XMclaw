"""Plugin system — third-party extension discovery and loading.

Public API:
  * :func:`xmclaw.plugins.loader.discover_plugins` — scan entry points.
  * :class:`xmclaw.plugins.loader.DiscoveryResult` — container for results.
  * :class:`xmclaw.plugins.loader.LoadedPlugin` — one loaded plugin.
"""
from __future__ import annotations

from xmclaw.plugins.loader import DiscoveryResult, LoadedPlugin, discover_plugins

__all__ = [
    "discover_plugins",
    "DiscoveryResult",
    "LoadedPlugin",
]
