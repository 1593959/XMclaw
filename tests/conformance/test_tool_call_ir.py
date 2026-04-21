"""Cross-provider ToolCall IR conformance — anti-req #3 + #14, CI-4.

Every translator must round-trip every ToolCall: encode → decode produces
an IR equal to the original (up to internal bookkeeping fields like
``provenance`` which is set by the decoder).

This matrix test is the CI hard-gate: a new provider cannot ship without
passing every case here.
"""
from __future__ import annotations

import pytest

from xmclaw.core.ir import ToolCall
from xmclaw.providers.llm.translators import (
    anthropic_native,
    openai_tool_shape,
)

TRANSLATORS = [
    ("anthropic_native", anthropic_native),
    ("openai_tool_shape", openai_tool_shape),
]


CALLS = [
    ToolCall(name="noop", args={}, provenance="synthetic"),
    ToolCall(name="file_read", args={"path": "/tmp/x"}, provenance="synthetic"),
    ToolCall(
        name="multi_arg",
        args={"a": 1, "b": "two", "c": [1, 2, 3], "d": {"nested": True}},
        provenance="synthetic",
    ),
    ToolCall(name="empty_string", args={"text": ""}, provenance="synthetic"),
    ToolCall(name="unicode_arg", args={"msg": "你好 🦞"}, provenance="synthetic"),
]


@pytest.mark.parametrize("name,translator", TRANSLATORS)
@pytest.mark.parametrize("call", CALLS)
def test_roundtrip_preserves_name_and_args(name: str, translator, call: ToolCall) -> None:
    block = translator.encode_to_provider(call)
    decoded = translator.decode_from_provider(block)
    assert decoded is not None, (
        f"{name}: round-trip produced None for {call.name!r}"
    )
    assert decoded.name == call.name
    assert decoded.args == call.args


@pytest.mark.parametrize("name,translator", TRANSLATORS)
def test_decode_none_for_malformed_inputs(name: str, translator) -> None:
    """Each translator MUST reject malformed inputs with None.

    This is anti-req #1 in its universal form: no translator is allowed to
    soft-parse a broken block into a ToolCall.
    """
    malformed_inputs = [
        None,
        "a string not a dict",
        123,
        [],
        {},  # empty dict has no type/function
    ]
    for bad in malformed_inputs:
        assert translator.decode_from_provider(bad) is None, (
            f"{name}: decoded malformed input {bad!r} instead of returning None"
        )


@pytest.mark.parametrize("name,translator", TRANSLATORS)
def test_encoded_shape_has_expected_keys(name: str, translator) -> None:
    call = ToolCall(name="x", args={"k": 1}, provenance="synthetic")
    block = translator.encode_to_provider(call)
    assert isinstance(block, dict)
    # Both Anthropic and OpenAI shapes have a "type" field; value differs.
    assert "type" in block
