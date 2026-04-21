"""Translate Anthropic ``tool_use`` blocks ↔ internal ``ToolCall``.

Anti-req #1 + #3: ``decode_from_provider`` MUST return structured ToolCall
or None. Never a "looks like a tool call" string. No soft-parse fallback.
"""
from __future__ import annotations

from typing import Any

from xmclaw.core.ir import ToolCall


def encode_to_provider(call: ToolCall) -> dict[str, Any]:  # noqa: ARG001
    """Convert internal ToolCall to Anthropic tool_use block."""
    raise NotImplementedError("Phase 2")


def decode_from_provider(block: dict[str, Any]) -> ToolCall | None:  # noqa: ARG001
    """Parse an Anthropic ``tool_use`` block. Return None if not a tool_use."""
    raise NotImplementedError("Phase 2")
