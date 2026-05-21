"""TUI — Jarvis Phase J3: terminal user interface.

Usage::

    xmclaw chat          # launch TUI (default)
    xmclaw chat --plain  # fallback to basic CLI
"""
from __future__ import annotations

from xmclaw.tui.app import JarvisTUI

__all__ = ["JarvisTUI"]
