"""Translate OpenAI ``tool_calls`` array ↔ internal ``ToolCall`` IR.

Anti-req #1 + #3: ``decode_from_provider`` MUST return a structured
``ToolCall`` or ``None``. No soft-parse fallback. The most common
OpenAI pitfall is that ``function.arguments`` is a JSON-encoded
*string*, not a dict — we parse it strictly; a malformed JSON string
is a decode failure, not a "try harder" case.

OpenAI tool-call wire shape (inbound):
    {"id": "call_x", "type": "function",
     "function": {"name": "foo", "arguments": "{\"k\": 1}"}}

OpenAI wire shape (outbound, when we send tool-call history back):
    {"id": "<ToolCall.id>", "type": "function",
     "function": {"name": "<name>", "arguments": "<json string>"}}
"""
from __future__ import annotations

import json
from typing import Any

from xmclaw.core.ir import ToolCall


def encode_to_provider(call: ToolCall) -> dict[str, Any]:
    """Convert internal ToolCall to an OpenAI tool_calls entry."""
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.name,
            "arguments": json.dumps(call.args),
        },
    }


def decode_from_provider(item: dict[str, Any]) -> ToolCall | None:
    """Parse an OpenAI tool_calls entry. Return None if not a valid function call.

    Strict: a non-dict, wrong type, missing function block, missing/empty
    name, or malformed arguments JSON all return None. Anti-req #1.
    """
    if not isinstance(item, dict):
        return None
    if item.get("type") != "function":
        return None
    fn = item.get("function")
    if not isinstance(fn, dict):
        return None
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        return None

    raw_args = fn.get("arguments")
    # OpenAI emits arguments as a JSON string. Some OpenAI-compat endpoints
    # (rarely, incorrectly) emit a dict directly — we accept that too, but
    # only after confirming it's a dict.
    args: dict[str, Any]
    if isinstance(raw_args, dict):
        args = raw_args
    elif isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        args = parsed
    elif raw_args is None:
        args = {}
    else:
        return None

    raw_id = item.get("id")
    kwargs: dict[str, Any] = {
        "name": name,
        "args": args,
        "provenance": "openai",
    }
    if isinstance(raw_id, str) and raw_id:
        kwargs["id"] = raw_id
    return ToolCall(**kwargs)
