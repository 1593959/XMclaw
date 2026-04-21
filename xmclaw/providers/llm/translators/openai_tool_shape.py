"""Translate OpenAI ``tool_calls`` array ↔ internal ``ToolCall``."""
from __future__ import annotations

from typing import Any

from xmclaw.core.ir import ToolCall


def encode_to_provider(call: ToolCall) -> dict[str, Any]:  # noqa: ARG001
    raise NotImplementedError("Phase 2")


def decode_from_provider(item: dict[str, Any]) -> ToolCall | None:  # noqa: ARG001
    raise NotImplementedError("Phase 2")
