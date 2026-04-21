"""Event formatter for ``xmclaw v2 chat`` — unit tests.

``format_event`` is pure (dict in, string out or None). These tests
pin the rendering contract: what the terminal user sees for each kind
of event that flows out of the daemon.
"""
from __future__ import annotations

from xmclaw.cli.v2_chat import RenderedLine, format_event


def _event(type_: str, payload: dict | None = None) -> dict:
    return {
        "id": "abc", "ts": 0.0, "session_id": "s", "agent_id": "a",
        "type": type_, "payload": payload or {},
    }


# ── suppressed event types ───────────────────────────────────────────────


def test_user_message_is_suppressed() -> None:
    """User already saw their own line at the prompt — no echo."""
    assert format_event(_event("user_message", {"content": "hi"})) is None


def test_llm_request_non_first_hop_suppressed() -> None:
    """We only surface thinking on hop 0; later hops stay silent so the
    terminal isn't drowned in '~thinking...' lines during tool loops."""
    assert format_event(_event("llm_request", {"hop": 1})) is None
    assert format_event(_event("llm_request", {"hop": 5})) is None


def test_llm_request_hop_zero_shows_thinking() -> None:
    line = format_event(_event("llm_request", {"hop": 0}))
    assert isinstance(line, RenderedLine)
    assert "thinking" in line.text.lower()


def test_llm_response_terminal_hop_renders_assistant_text() -> None:
    """Terminal LLM_RESPONSE (no tool calls) carries the assistant's
    final answer. Chat renders it as the user-visible reply."""
    line = format_event(_event("llm_response", {
        "ok": True, "tool_calls_count": 0,
        "content": "Hello! How can I help you today?",
    }))
    assert isinstance(line, RenderedLine)
    assert line.is_assistant is True
    assert "Hello!" in line.text
    assert "agent" in line.text.lower() or "◉" in line.text


def test_llm_response_pre_tool_hop_suppressed() -> None:
    """If a tool call is about to follow, the LLM's short narration is
    noise next to the TOOL_CALL_EMITTED event that comes right after."""
    line = format_event(_event("llm_response", {
        "ok": True, "tool_calls_count": 1,
        "content": "Let me check the file",
    }))
    assert line is None


def test_llm_response_ok_empty_content_suppressed() -> None:
    """Some models emit LLM_RESPONSE with empty content when they're
    solely calling a tool — render nothing."""
    line = format_event(_event("llm_response", {
        "ok": True, "tool_calls_count": 0, "content": "",
    }))
    assert line is None


def test_llm_response_error_surfaced() -> None:
    line = format_event(_event("llm_response", {
        "ok": False, "error": "Rate limit",
    }))
    assert isinstance(line, RenderedLine)
    assert "rate limit" in line.text.lower()
    assert "⚠" in line.text or "error" in line.text.lower()


# ── tool events ──────────────────────────────────────────────────────────


def test_tool_call_emitted_shows_name_and_args() -> None:
    line = format_event(_event("tool_call_emitted", {
        "name": "file_read", "args": {"path": "/tmp/x.txt"},
    }))
    assert isinstance(line, RenderedLine)
    assert "file_read" in line.text
    assert "/tmp/x.txt" in line.text
    assert line.text.startswith("  →")


def test_tool_call_emitted_truncates_long_args() -> None:
    long_content = "a" * 200
    line = format_event(_event("tool_call_emitted", {
        "name": "file_write",
        "args": {"path": "/tmp/x", "content": long_content},
    }))
    assert isinstance(line, RenderedLine)
    # Line shouldn't explode past a reasonable width.
    assert len(line.text) < 200
    assert "..." in line.text


def test_tool_invocation_finished_ok_pure_read() -> None:
    line = format_event(_event("tool_invocation_finished", {
        "name": "file_read", "ok": True,
        "result": "document contents here",
        "expected_side_effects": [],
    }))
    assert isinstance(line, RenderedLine)
    assert "file_read" in line.text
    assert "ok" in line.text
    assert "←" in line.text


def test_tool_invocation_finished_ok_with_side_effects() -> None:
    """Write tools show the wrote-path so the user sees what landed."""
    line = format_event(_event("tool_invocation_finished", {
        "name": "file_write", "ok": True,
        "result": {"path": "/tmp/greeting.txt", "bytes": 10},
        "expected_side_effects": ["/tmp/greeting.txt"],
    }))
    assert isinstance(line, RenderedLine)
    assert "file_write" in line.text
    assert "/tmp/greeting.txt" in line.text


def test_tool_invocation_finished_failure_surfaces_error() -> None:
    line = format_event(_event("tool_invocation_finished", {
        "name": "file_read", "ok": False,
        "error": "permission denied: /etc/passwd",
    }))
    assert isinstance(line, RenderedLine)
    assert "failed" in line.text.lower()
    assert "permission" in line.text.lower()


def test_tool_invocation_finished_long_result_truncated() -> None:
    huge = "x" * 1000
    line = format_event(_event("tool_invocation_finished", {
        "name": "file_read", "ok": True,
        "result": huge, "expected_side_effects": [],
    }))
    assert isinstance(line, RenderedLine)
    assert len(line.text) < 200
    assert "..." in line.text


# ── lifecycle + violations ───────────────────────────────────────────────


def test_anti_req_violation_rendered_with_warning() -> None:
    line = format_event(_event("anti_req_violation", {
        "message": "max_hops exceeded",
    }))
    assert isinstance(line, RenderedLine)
    assert "max_hops" in line.text
    assert "⚠" in line.text or "violation" in line.text.lower()


def test_session_lifecycle_create_rendered() -> None:
    line = format_event(_event("session_lifecycle", {"phase": "create"}))
    assert isinstance(line, RenderedLine)
    assert "opened" in line.text.lower() or "create" in line.text.lower()


def test_session_lifecycle_destroy_rendered() -> None:
    line = format_event(_event("session_lifecycle", {"phase": "destroy"}))
    assert isinstance(line, RenderedLine)
    assert "closed" in line.text.lower() or "destroy" in line.text.lower()


def test_session_lifecycle_unknown_phase_silent() -> None:
    assert format_event(_event("session_lifecycle", {"phase": "weird"})) is None


# ── unknown event type ───────────────────────────────────────────────────


def test_unknown_event_type_suppressed() -> None:
    """Forward-compat — if the daemon adds a new event type the client
    doesn't know, we stay silent rather than spam the terminal with
    unexpected JSON."""
    assert format_event(_event("some_future_type", {"x": 1})) is None


def test_malformed_event_doesnt_crash() -> None:
    """Missing payload / missing type must not explode."""
    assert format_event({"type": "anti_req_violation"}) is not None  # no payload
    assert format_event({}) is None  # no type
