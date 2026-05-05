"""B-226: tool result pruning unit tests.

Pin the algorithm so a refactor doesn't accidentally break the
long-conversation token budget. Mirrors the pattern used in
``test_v2_proposal_materializer.py`` — small in-memory fixtures,
no DB, no network.
"""
from __future__ import annotations

import json

from xmclaw.core.ir import ToolCall
from xmclaw.providers.llm.base import Message
from xmclaw.context.tool_result_prune import (
    _summarize_tool_result,
    prune_old_tool_results,
)


def _assistant_with_tool(call_id: str, name: str, args: dict) -> Message:
    """Helper — assistant turn with one tool_call."""
    return Message(
        role="assistant",
        content="",
        tool_calls=(ToolCall(
            id=call_id, name=name, args=args, provenance="anthropic_native",
        ),),
    )


def _tool_result(call_id: str, content: str) -> Message:
    return Message(role="tool", content=content, tool_call_id=call_id)


# ── _summarize_tool_result ────────────────────────────────────────


def test_summarize_bash() -> None:
    out = _summarize_tool_result(
        "bash",
        {"command": "ls -la"},
        '{"exit_code": 0, "stdout": "a\\nb\\nc"}',
    )
    assert "[bash]" in out
    assert "ls -la" in out
    assert "exit 0" in out


def test_summarize_file_read() -> None:
    out = _summarize_tool_result(
        "file_read",
        {"path": "/foo/bar.py", "offset": 1},
        "x" * 5000,
    )
    assert "[file_read]" in out
    assert "/foo/bar.py" in out
    assert "5,000 chars" in out


def test_summarize_grep_files() -> None:
    out = _summarize_tool_result(
        "grep_files",
        {"pattern": "TODO", "path": "src/"},
        '{"total_matches": 42, "files": []}',
    )
    assert "[grep_files]" in out
    assert "'TODO'" in out
    assert "42 matches" in out


def test_summarize_unknown_tool() -> None:
    out = _summarize_tool_result("foobar_tool", {}, "abc" * 100)
    # Unknown → generic ``[name] (N chars)``
    assert "[foobar_tool]" in out
    assert "300 chars" in out


def test_summarize_falls_back_when_args_unparseable() -> None:
    """Real-data: tool_calls.args sometimes arrive as a string blob,
    sometimes as a dict. _summarize_tool_result must handle both."""
    out = _summarize_tool_result("bash", "not-json-{[", "out")
    # No crash, returns something sensible.
    assert "[bash]" in out


# ── prune_old_tool_results ────────────────────────────────────────


def test_prune_skips_short_messages() -> None:
    """tool result < 200 chars stays intact (already small)."""
    msgs = [
        Message(role="user", content="hi"),
        _assistant_with_tool("c1", "bash", {"command": "echo hi"}),
        _tool_result("c1", "hi"),  # 2 chars — not pruned
    ]
    out, pruned = prune_old_tool_results(
        msgs, protect_tail_tokens=10, protect_tail_count_floor=1,
    )
    assert pruned == 0
    assert out[2].content == "hi"


def test_prune_replaces_old_tool_with_summary() -> None:
    """An OLD tool result > 200 chars gets summarized, NEW tail
    kept intact."""
    big = "x" * 1000
    fresh = "y" * 1000
    msgs = [
        Message(role="user", content="task A"),
        _assistant_with_tool("c1", "file_read", {"path": "old.py"}),
        _tool_result("c1", big),                    # OLD — should prune
        Message(role="user", content="task B"),
        _assistant_with_tool("c2", "file_read", {"path": "new.py"}),
        _tool_result("c2", fresh),                  # FRESH tail — keep
    ]
    out, pruned = prune_old_tool_results(
        msgs,
        protect_tail_tokens=200,  # tight budget so only ~last 1-2 protected
        protect_tail_count_floor=2,
    )
    # The old tool result got replaced
    assert pruned >= 1
    assert out[2].content != big  # was rewritten
    assert "[file_read]" in out[2].content
    assert "old.py" in out[2].content
    # Fresh tail intact
    assert out[5].content == fresh


def test_prune_dedupes_identical_tool_results() -> None:
    """Same content read twice — older one becomes a back-reference,
    newest stays full."""
    blob = "z" * 800
    msgs = [
        _assistant_with_tool("c1", "file_read", {"path": "x.py"}),
        _tool_result("c1", blob),  # OLD copy
        Message(role="user", content="check it again"),
        _assistant_with_tool("c2", "file_read", {"path": "x.py"}),
        _tool_result("c2", blob),  # FRESH copy (same content)
    ]
    out, pruned = prune_old_tool_results(
        msgs, protect_tail_tokens=10000, protect_tail_count_floor=10,
    )
    # The OLDER copy got back-referenced (Pass 1 dedup)
    old_text = out[1].content
    new_text = out[4].content
    assert "Duplicate tool output" in old_text
    assert new_text == blob  # newest preserved
    assert pruned >= 1


def test_prune_empty_input() -> None:
    out, pruned = prune_old_tool_results([])
    assert out == []
    assert pruned == 0


def test_prune_no_assistant_lookup_when_orphan_tool() -> None:
    """A tool message whose call_id has no matching assistant tool_call
    still gets summarized (fallback to ``unknown``)."""
    msgs = [
        Message(role="user", content="hi"),
        _tool_result("orphan", "x" * 500),  # orphan tool result
    ]
    out, pruned = prune_old_tool_results(
        msgs, protect_tail_tokens=10, protect_tail_count_floor=1,
    )
    # Orphan still gets pruned with [unknown] fallback summary
    if pruned >= 1:
        assert "[unknown]" in out[1].content or "[tool]" in out[1].content


def test_prune_preserves_user_and_assistant_text() -> None:
    """User + assistant text content is NEVER touched — only
    tool messages get rewritten."""
    msgs = [
        Message(role="user", content="user text " * 100),
        Message(role="assistant", content="assistant text " * 100),
        _assistant_with_tool("c1", "bash", {"command": "ls"}),
        _tool_result("c1", "out " * 100),
        Message(role="user", content="next user"),
    ]
    out, _ = prune_old_tool_results(
        msgs, protect_tail_tokens=50, protect_tail_count_floor=1,
    )
    assert out[0].content == msgs[0].content  # user untouched
    assert out[1].content == msgs[1].content  # assistant untouched


def test_prune_returns_independent_list() -> None:
    """Caller's input list must not be mutated."""
    blob = "x" * 1000
    msgs = [
        _assistant_with_tool("c1", "bash", {"command": "ls"}),
        _tool_result("c1", blob),
        Message(role="user", content="next"),
    ]
    original_content = msgs[1].content
    out, _ = prune_old_tool_results(
        msgs, protect_tail_tokens=20, protect_tail_count_floor=1,
    )
    # Input list intact
    assert msgs[1].content == original_content
    # Output may differ
    assert out[1].content != original_content or "[bash]" in out[1].content
