"""Media generation providers — image / video / audio synthesis.

These are NOT ToolProviders themselves; they are internal helpers that
encapsulate vendor-specific APIs (OpenAI DALL-E, Replicate, etc.).
The ToolProvider wrappers live in ``xmclaw.providers.tool``.
"""
from __future__ import annotations

from xmclaw.providers.media.dalle3 import Dalle3Provider

__all__ = ["Dalle3Provider"]
