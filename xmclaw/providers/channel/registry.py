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
#
# B-330: ``acp`` joined the list — it lives in ``acp.py`` (single-file
# package, not a sub-directory) but the discover loop's
# ``importlib.import_module(f"xmclaw.providers.channel.{cid}")``
# handles both shapes. Pre-B-330 ACP's MANIFEST was defined but the
# id wasn't in CHANNEL_IDS, so ``discover()`` never saw it — the
# ``acp.py`` docstring claiming "the UI shows ACP as available" was
# stale. Now ACP appears in ``include_scaffolds=True`` output so the
# Channels page can render it as "coming soon" alongside the 4 IM
# scaffolds; ``include_scaffolds=False`` (default) hides it because
# the manifest carries ``implementation_status="scaffold"``.
CHANNEL_IDS: tuple[str, ...] = (
    "feishu",
    "dingtalk",
    "wecom",
    "weixin",
    "telegram",
    "acp",
)


def discover(*, include_scaffolds: bool = False) -> dict[str, PluginManifest]:
    """Return ``{channel_id: PluginManifest}`` for every package that
    exposes a valid ``MANIFEST``. Skips broken packages with a warning.

    B-38: by default, scaffold-only manifests (those whose
    ``implementation_status != "ready"``) are filtered out so the
    daemon doesn't advertise phantom channels in the UI. Pass
    ``include_scaffolds=True`` to get the raw set — useful for the
    Channels page that wants to render scaffolds as grayed-out
    "coming soon" entries.
    """
    out: dict[str, PluginManifest] = {}
    for cid in CHANNEL_IDS:
        try:
            mod = importlib.import_module(f"xmclaw.providers.channel.{cid}")
        except ImportError:
            continue
        manifest = getattr(mod, "MANIFEST", None)
        if not isinstance(manifest, PluginManifest):
            continue
        if (not include_scaffolds
                and getattr(manifest, "implementation_status", "ready") != "ready"):
            continue
        out[manifest.id] = manifest
    return out


def needs_tunnel(enabled_ids: Iterable[str]) -> bool:
    """Should the daemon auto-start cloudflared given this enable list?

    Consults scaffolds too — the tunnel decision happens before
    adapter import, and configured-but-scaffold channels still
    declare their tunnel needs in the manifest. Filtering scaffolds
    out of the decision would mean re-enabling a channel after its
    adapter ships would silently miss the tunnel auto-start.
    """
    manifests = discover(include_scaffolds=True)
    for cid in enabled_ids:
        m = manifests.get(cid)
        if m is not None and m.needs_tunnel:
            return True
    return False
