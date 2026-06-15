"""#6 trace export/replay — JSONL round-trip + timeline reconstruction."""
from __future__ import annotations

from xmclaw.daemon.trace_replay import (
    events_to_jsonl,
    jsonl_to_events,
    reconstruct_timeline,
)


_TRACE = [
    {"type": "user_message", "payload": {"content": "build X"}, "ts": 1.0},
    {"type": "llm_response", "payload": {
        "text": "", "tool_calls": [{"name": "file_read", "args": {"path": "a.py"}}],
    }, "ts": 2.0},
    {"type": "tool_invocation_started", "payload": {"name": "file_read"}, "ts": 2.1},
    {"type": "tool_invocation_finished", "payload": {"name": "file_read", "ok": True, "result": "contents"}, "ts": 2.5},
    {"type": "user_message", "payload": {"content": "actually use Y", "channel": "steering"}, "ts": 3.0},
    {"type": "llm_response", "payload": {"text": "done with Y", "tool_calls": []}, "ts": 4.0},
    {"type": "session_lifecycle", "payload": {"phase": "turn_cancelled"}, "ts": 5.0},
]


def test_jsonl_roundtrip() -> None:
    text = events_to_jsonl(_TRACE)
    assert text.count("\n") == len(_TRACE) - 1  # one line per event
    back = jsonl_to_events(text)
    assert back == _TRACE


def test_jsonl_skips_blank_and_garbage_lines() -> None:
    text = events_to_jsonl(_TRACE[:2]) + "\n\nnot json\n"
    back = jsonl_to_events(text)
    assert len(back) == 2  # blank + garbage dropped


def test_reconstruct_timeline_renders_key_events() -> None:
    out = reconstruct_timeline(_TRACE)
    assert "USER: build X" in out
    assert "call file_read" in out
    assert "tool[file_read] ok=True" in out
    # steering is visually distinguished from a normal user message
    assert "USER·steer: actually use Y" in out
    assert "ASSISTANT: done with Y" in out
    assert "turn_cancelled" in out


def test_reconstruct_tolerates_unknown_and_malformed() -> None:
    weird = [
        {"type": "some_future_event", "payload": {"x": 1}},
        {"type": "llm_response"},          # no payload
        "not-a-dict",                       # garbage entry
        {"type": "tool_invocation_finished", "payload": {"tool_name": "bash", "ok": False, "error": "boom"}},
    ]
    out = reconstruct_timeline(weird)  # must not raise
    assert "tool[bash] ok=False" in out
    assert "boom" in out
