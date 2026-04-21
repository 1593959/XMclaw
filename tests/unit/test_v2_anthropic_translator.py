"""Anthropic translator — double-direction fuzz (anti-req #3, CI-4).

Every ``ToolCall`` that we encode must round-trip back to an equal
``ToolCall`` through ``decode_from_provider``. Malformed blocks must
return ``None`` rather than a soft-parsed result (anti-req #1).
"""
from __future__ import annotations

from xmclaw.core.ir import ToolCall
from xmclaw.providers.llm.translators import anthropic_native as translator


def test_encode_roundtrip_preserves_name_and_args() -> None:
    call = ToolCall(
        name="file_read",
        args={"path": "/tmp/x", "lines": 10},
        provenance="synthetic",
    )
    block = translator.encode_to_provider(call)
    decoded = translator.decode_from_provider(block)
    assert decoded is not None
    assert decoded.name == call.name
    assert decoded.args == call.args
    # id is preserved through the round-trip
    assert decoded.id == call.id


def test_encode_emits_tool_use_type() -> None:
    block = translator.encode_to_provider(
        ToolCall(name="x", args={}, provenance="synthetic"),
    )
    assert block["type"] == "tool_use"


def test_decode_rejects_non_dict() -> None:
    assert translator.decode_from_provider("not a dict") is None  # type: ignore[arg-type]
    assert translator.decode_from_provider(None) is None  # type: ignore[arg-type]


def test_decode_rejects_wrong_type() -> None:
    assert translator.decode_from_provider({"type": "text", "text": "hi"}) is None


def test_decode_rejects_missing_name() -> None:
    assert translator.decode_from_provider(
        {"type": "tool_use", "id": "x", "input": {}},
    ) is None


def test_decode_rejects_empty_name() -> None:
    assert translator.decode_from_provider(
        {"type": "tool_use", "id": "x", "name": "", "input": {}},
    ) is None


def test_decode_rejects_non_dict_input() -> None:
    assert translator.decode_from_provider(
        {"type": "tool_use", "id": "x", "name": "f", "input": "not-a-dict"},
    ) is None


def test_decode_accepts_missing_id_and_generates_new_one() -> None:
    decoded = translator.decode_from_provider(
        {"type": "tool_use", "name": "f", "input": {}},
    )
    assert decoded is not None
    # A fresh uuid was generated
    assert len(decoded.id) > 0


def test_decoded_has_provenance_anthropic() -> None:
    decoded = translator.decode_from_provider(
        {"type": "tool_use", "id": "toolu_x", "name": "f", "input": {"k": 1}},
    )
    assert decoded is not None
    assert decoded.provenance == "anthropic"


def test_decode_no_soft_parse_fallback() -> None:
    """Anti-req #1: text that DESCRIBES a tool call is never decoded as one."""
    # A literal string, a JSON string, a dict with text — none should pass.
    assert translator.decode_from_provider('{"name": "foo", "input": {}}') is None  # type: ignore[arg-type]
    assert translator.decode_from_provider(
        {"type": "text", "text": "I will call foo with input {}"},
    ) is None
