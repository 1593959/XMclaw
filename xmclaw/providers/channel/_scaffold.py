"""Shared scaffold helpers for channel adapters that haven't been
ported yet (B-329).

The four Chinese IM adapters (telegram / dingtalk / wecom / weixin)
ship manifests with ``implementation_status="scaffold"`` and a
``adapter_factory_path`` pointing at an ``adapter.py`` that didn't
exist before B-329. The discover-side filter (``include_scaffolds=False``)
keeps the dispatch path from ever importing those modules in normal
operation, but anyone using ``include_scaffolds=True`` (tests,
future code, debug tooling) hit a cryptic ``ModuleNotFoundError:
xmclaw.providers.channel.telegram.adapter`` instead of an explanation.

This helper provides :class:`ScaffoldChannelAdapter` so each scaffolded
package can ship a tiny ``adapter.py`` that:

* exists (so ``__import__`` succeeds — the manifest's
  ``adapter_factory_path`` resolves),
* surfaces a clear, actionable ``NotImplementedError`` on
  instantiation that names the channel + the upstream agent port target.

Manifest discovery is unaffected — the ``__init__.py`` files import
nothing from here.
"""
from __future__ import annotations

from typing import Any


class ScaffoldChannelAdapter:
    """Drop-in adapter stub for scaffolded channels. Raises
    ``NotImplementedError`` on construction with a message that
    points at the upstream agent port target + tells the operator how to
    contribute or ask.

    Subclasses set ``CHANNEL_NAME`` and ``PORT_TARGET`` so the error
    is specific to the channel.
    """

    #: Human-readable channel name shown in the error (e.g. "Telegram").
    CHANNEL_NAME: str = "<unknown>"
    #: Path to the upstream agent reference adapter to port.
    PORT_TARGET: str = "<not specified>"
    #: Optional one-line note about credentials / mode.
    EXTRA_NOTE: str = ""

    def __init__(self, _config: dict[str, Any] | None = None) -> None:
        msg = (
            f"{type(self).__name__} ({self.CHANNEL_NAME}) is a scaffold — "
            f"the manifest exists so the channel shows up in the UI / "
            f"channel registry, but the adapter logic hasn't been ported "
            f"yet. Port reference: {self.PORT_TARGET}."
        )
        if self.EXTRA_NOTE:
            msg += f" {self.EXTRA_NOTE}"
        msg += (
            " The dispatcher's discover() defaults to "
            "include_scaffolds=False, so configuring this channel in "
            "production is normally filtered out before reaching this "
            "constructor. If you got here, you're either running with "
            "include_scaffolds=True, importing the adapter directly, or "
            "running on a build where the scaffold-filter regressed — "
            "open an issue."
        )
        raise NotImplementedError(msg)
