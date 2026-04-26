"""Channel manifest registry — discover available channel plugins.

Mirrors OpenClaw's ``getChannelPlugin`` (``src/agents/channels/plugins/
index.ts``). Each channel package exposes a module-level ``MANIFEST:
PluginManifest``; this module enumerates them so the daemon can:

  * Render a "Channels" UI list with status (configured / not / running)
  * Decide whether to auto-start cloudflared (any enabled channel with
    ``needs_tunnel=True`` triggers the bootstrap)
  * Lazy-import adapter classes only when a channel is actually enabled
"""
from __future__ import annotations

import importlib
from typing import Iterable

from xmclaw.providers.channel.base import PluginManifest

# Canonical channel ids in priority order (Chinese-market first per
# user's positioning).
CHANNEL_IDS: tuple[str, ...] = (
    "feishu",
    "dingtalk",
    "wecom",
    "weixin",
    "telegram",
)


def discover() -> dict[str, PluginManifest]:
    """Return ``{channel_id: PluginManifest}`` for every package that
    exposes a valid ``MANIFEST``. Skips broken packages with a warning."""
    out: dict[str, PluginManifest] = {}
    for cid in CHANNEL_IDS:
        try:
            mod = importlib.import_module(f"xmclaw.providers.channel.{cid}")
        except ImportError:
            continue
        manifest = getattr(mod, "MANIFEST", None)
        if isinstance(manifest, PluginManifest):
            out[manifest.id] = manifest
    return out


def needs_tunnel(enabled_ids: Iterable[str]) -> bool:
    """Should the daemon auto-start cloudflared given this enable list?"""
    manifests = discover()
    for cid in enabled_ids:
        m = manifests.get(cid)
        if m is not None and m.needs_tunnel:
            return True
    return False
