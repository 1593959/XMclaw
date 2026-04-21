"""MemoryProvider interface + default sqlite-vec implementation.

Anti-req #2: layered (short/working/long), semantic retrieval, NOT frozen
into the system prompt. See V2_DEVELOPMENT.md §3.2.
"""
from xmclaw.providers.memory.base import Layer, MemoryItem, MemoryProvider

__all__ = ["Layer", "MemoryItem", "MemoryProvider"]
