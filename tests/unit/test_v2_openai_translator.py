"""OpenAI tool-shape translator — double-direction fuzz (anti-req #3, CI-4).

Parallels ``test_v2_anthropic_translator.py``. The OpenAI-specific gotcha:
``function.arguments`` is a JSON-encoded string. Malformed JSON is a decode
failure, not a soft-parse case — anti-req #1.
"""
from __future__ import annotations

import json

from xmclaw.core.ir import ToolCall
from xmclaw.providers.llm.translators import openai_tool_shape as translator


# ── happy-path round-trip ─────────────────────────────────────────────────

def test_encode_roundtrip_preserves_name_and_args() -> None:
    call = ToolCall(
        name="file_read",
        args={"path": "/tmp/x", "lines": 10},
        provenance="synthetic",
        id="call-xyz",
    )
    entry = translator.encode_to_provider(call)
    decoded = translator.decode_from_provider(entry)
    assert decoded is not None
    assert decoded.name == call.name
    assert decoded.args == call.args
    assert decoded.id == call.id


def test_encode_emits_function_type_and_json_string_args() -> None:
    entry = translator.encode_to_provider(
        ToolCall(name="x", args={"a": 1, "b": "two"}, provenance="synthetic"),
    )
    assert entry["type"] == "function"
    # arguments MUST be a JSON string per OpenAI spec
    assert isinstance(entry["function"]["arguments"], str)
    assert json.loads(entry["function"]["arguments"]) == {"a": 1, "b": "two"}


# ── decode rejections (anti-req #1) ───────────────────────────────────────

def test_decode_rejects_non_dict() -> None:
    assert translator.decode_from_provider("not a dict") is None  # type: ignore[arg-type]
    assert translator.decode_from_provider(None) is None  # type: ignore[arg-type]


def test_decode_rejects_wrong_type() -> None:
    assert translator.decode_from_provider(
        {"id": "x", "type": "not_function", "function": {"name": "f", "arguments": "{}"}},
    ) is None


def test_decode_rejects_missing_function_block() -> None:
    assert translator.decode_from_provider(
        {"id": "x", "type": "function"},
    ) is None


def test_decode_rejects_non_dict_function_block() -> None:
    assert translator.decode_from_provider(
        {"id": "x", "type": "function", "function": "oops"},
    ) is None


def test_decode_rejects_missing_name() -> None:
    assert translator.decode_from_provider(
        {"id": "x", "type": "function", "function": {"arguments": "{}"}},
    ) is None


def test_decode_rejects_empty_name() -> None:
    assert translator.decode_from_provider(
        {"id": "x", "type": "function", "function": {"name": "", "arguments": "{}"}},
    ) is None


def test_decode_rejects_malformed_json_arguments() -> None:
    assert translator.decode_from_provider(
        {"id": "x", "type": "function", "function": {"name": "f", "arguments": "{not json"}},
    ) is None


def test_decode_rejects_json_that_is_not_an_object() -> None:
    assert translator.decode_from_provider(
        {"id": "x", "type": "function", "function": {"name": "f", "arguments": "[1,2,3]"}},
    ) is None


def test_decode_rejects_non_string_non_dict_args() -> None:
    # some edge case — arguments as int
    assert translator.decode_from_provider(
        {"id": "x", "type": "function", "function": {"name": "f", "arguments": 42}},
    ) is None


# ── decode accepts lenient cases ──────────────────────────────────────────

def test_decode_accepts_dict_arguments_from_compat_endpoint() -> None:
    """Some OpenAI-compat servers return a dict directly (spec violation
    but widespread). We accept it rather than refuse."""
    decoded = translator.decode_from_provider(
        {"id": "x", "type": "function", "function": {"name": "f", "arguments": {"k": 1}}},
    )
    assert decoded is not None
    assert decoded.args == {"k": 1}


def test_decode_rejects_empty_arguments_string() -> None:
    """B-229: empty STRING ``""`` only appears as the initial state of
    the streaming accumulator — when it persists into a finalised
    response the stream got truncated mid-tool-call. A legitimate
    zero-args call serialises as ``"{}"``, never ``""``. Returning
    ``None`` here is what stops the ``code_python({})`` ghost-call
    bug from reaching the agent loop's invocation step."""
    decoded = translator.decode_from_provider(
        {"id": "x", "type": "function", "function": {"name": "f", "arguments": ""}},
    )
    assert decoded is None


def test_decode_accepts_empty_dict_arguments_string() -> None:
    """A legitimate zero-args call (``"{}"``) still parses as before."""
    decoded = translator.decode_from_provider(
        {"id": "x", "type": "function", "function": {"name": "f", "arguments": "{}"}},
    )
    assert decoded is not None
    assert decoded.args == {}


def test_decode_accepts_null_arguments() -> None:
    decoded = translator.decode_from_provider(
        {"id": "x", "type": "function", "function": {"name": "f", "arguments": None}},
    )
    assert decoded is not None
    assert decoded.args == {}


def test_decode_accepts_missing_id_and_generates_new_one() -> None:
    decoded = translator.decode_from_provider(
        {"type": "function", "function": {"name": "f", "arguments": "{}"}},
    )
    assert decoded is not None
    assert len(decoded.id) > 0


def test_decoded_has_provenance_openai() -> None:
    decoded = translator.decode_from_provider(
        {"id": "call_x", "type": "function",
         "function": {"name": "f", "arguments": "{\"k\":1}"}},
    )
    assert decoded is not None
    assert decoded.provenance == "openai"


# ── anti-req #1: no soft-parse fallback ───────────────────────────────────

def test_decode_rejects_plain_text_that_looks_like_a_tool_call() -> None:
    assert translator.decode_from_provider(
        '{"id": "x", "type": "function"}',  # a string, not a dict
    ) is None  # type: ignore[arg-type]
    assert translator.decode_from_provider(
        {"text": "Call function f with {}"},  # doesn't match shape
    ) is None
