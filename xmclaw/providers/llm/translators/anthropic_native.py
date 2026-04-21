"""Translate Anthropic ``tool_use`` blocks ↔ internal ``ToolCall`` IR.

Anti-req #1 + #3: ``decode_from_provider`` MUST return a structured
``ToolCall`` or ``None``. Never a "looks like a tool call" string. There
is no soft-parse fallback — if the block isn't a valid ``tool_use``, we
return ``None`` and let the caller log an ``anti_req_violation`` event.

Anthropic block shape (inbound):
    {"type": "tool_use", "id": "toolu_x", "name": "foo", "input": {...}}

Anthropic block shape (outbound, what we emit when sending tool-call
history back to the model):
    {"type": "tool_use", "id": "<ToolCall.id>", "name": "<name>",
     "input": <args dict>}
"""
from __future__ import annotations

from typing import Any

from xmclaw.core.ir import ToolCall


def encode_to_provider(call: ToolCall) -> dict[str, Any]:
    """Convert internal ToolCall to an Anthropic tool_use block."""
    return {
        "type": "tool_use",
        "id": call.id,
        "name": call.name,
        "input": dict(call.args),
    }


def decode_from_provider(block: dict[str, Any]) -> ToolCall | None:
    """Parse an Anthropic ``tool_use`` block.

    Returns ``None`` if the block does not match the expected shape.
    Anti-req #1: we refuse to "best-effort" parse malformed blocks.
    """
    if not isinstance(block, dict):
        return None
    if block.get("type") != "tool_use":
        return None
    name = block.get("name")
    if not isinstance(name, str) or not name:
        return None
    args = block.get("input")
    if not isinstance(args, dict):
        return None
    raw_id = block.get("id")
    # Allow missing id (synthetic origin) but only if it's None or empty-string
    # we'll generate a fresh one via ToolCall's default factory.
    kwargs: dict[str, Any] = {
        "name": name,
        "args": dict(args),
        "provenance": "anthropic",
    }
    if isinstance(raw_id, str) and raw_id:
        kwargs["id"] = raw_id
    return ToolCall(**kwargs)
