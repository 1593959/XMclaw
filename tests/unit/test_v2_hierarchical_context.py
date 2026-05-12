"""Unit tests for HierarchicalContextWindow (Batch A.2 — subgoal-aware
history compression).

Tests cover ``_group_into_subgoals`` and ``_build_compression_summary_rule_based``
behaviour: messages get bucketed by user turn, tool calls are counted
per subgoal (ok / fail), assistant synthesis text is captured, and the
final markdown digest reflects the structure.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from xmclaw.daemon.history_compression import HistoryCompressionMixin


def _m(role, content="", tool_calls=()):
    """Minimal Message-shape for tests — duck-typed to what the
    compressor reads (role / content / tool_calls)."""
    return SimpleNamespace(role=role, content=content, tool_calls=tool_calls)


def _tc(name):
    return SimpleNamespace(name=name)


# ── _group_into_subgoals ─────────────────────────────────────────


def test_group_empty():
    assert HistoryCompressionMixin._group_into_subgoals([]) == []


def test_group_single_subgoal_no_tools():
    msgs = [
        _m("user", "Find TODOs"),
        _m("assistant", "Found 5 TODOs in main.py"),
    ]
    out = HistoryCompressionMixin._group_into_subgoals(msgs)
    assert len(out) == 1
    sg = out[0]
    assert sg["user_text"] == "Find TODOs"
    assert sg["tool_count"] == 0
    assert sg["assistant_synthesis"] == "Found 5 TODOs in main.py"


def test_group_two_subgoals_split_by_user_turn():
    msgs = [
        _m("user", "First task"),
        _m("assistant", "Done with first"),
        _m("user", "Second task"),
        _m("assistant", "Done with second"),
    ]
    out = HistoryCompressionMixin._group_into_subgoals(msgs)
    assert len(out) == 2
    assert out[0]["user_text"] == "First task"
    assert out[1]["user_text"] == "Second task"
    assert out[0]["assistant_synthesis"] == "Done with first"
    assert out[1]["assistant_synthesis"] == "Done with second"


def test_group_counts_tool_calls_per_subgoal():
    msgs = [
        _m("user", "Read 3 files"),
        _m("assistant", "I'll read them", tool_calls=(
            _tc("file_read"), _tc("file_read"), _tc("file_read"),
        )),
        _m("tool", "<file1 content>"),
        _m("tool", "<file2 content>"),
        _m("tool", "<file3 content>"),
        _m("assistant", "Read all 3 files. Here's the summary..."),
    ]
    out = HistoryCompressionMixin._group_into_subgoals(msgs)
    assert len(out) == 1
    sg = out[0]
    assert sg["tool_count"] == 3
    assert sg["tool_ok"] == 3
    assert sg["tool_fail"] == 0
    assert sg["tool_names"] == ["file_read"]  # dedup
    assert "summary" in sg["assistant_synthesis"]


def test_group_detects_tool_failures():
    msgs = [
        _m("user", "Write to a restricted file"),
        _m("assistant", "trying", tool_calls=(_tc("file_write"),)),
        _m("tool", "permission denied on /etc/passwd"),
        _m("assistant", "Sorry, I can't write there."),
    ]
    out = HistoryCompressionMixin._group_into_subgoals(msgs)
    sg = out[0]
    assert sg["tool_ok"] == 0
    assert sg["tool_fail"] == 1


def test_group_handles_pre_user_orphans():
    """If history starts with a non-user message (e.g. system migration
    artifact), it gets stuffed into a placeholder subgoal labelled
    ``(pre-user context)``."""
    msgs = [
        _m("assistant", "orphan"),
        _m("user", "Real start"),
        _m("assistant", "OK"),
    ]
    out = HistoryCompressionMixin._group_into_subgoals(msgs)
    assert len(out) == 2
    assert out[0]["user_text"] == "(pre-user context)"
    assert out[1]["user_text"] == "Real start"


def test_group_preserves_tool_name_order():
    """First-seen-first ordering on tool_names list."""
    msgs = [
        _m("user", "u"),
        _m("assistant", "x", tool_calls=(
            _tc("grep_files"), _tc("file_read"),
            _tc("file_read"), _tc("bash"),
        )),
    ]
    out = HistoryCompressionMixin._group_into_subgoals(msgs)
    assert out[0]["tool_names"] == ["grep_files", "file_read", "bash"]


# ── _build_compression_summary_rule_based ────────────────────────


class _StubMixin(HistoryCompressionMixin):
    """Minimal harness that lets us call the method without building
    a full AgentLoop. Compressor doesn't access self._llm /
    self._memory_manager from the rule-based path so we leave them None."""

    def __init__(self):
        self._llm = None
        self._memory_manager = None
        self._pending_llm_compression = {}


def test_summary_empty_dropped():
    h = _StubMixin()
    assert h._build_compression_summary_rule_based([], "") == ""


def test_summary_includes_subgoal_sections():
    h = _StubMixin()
    msgs = [
        _m("user", "Task 1: find TODOs"),
        _m("assistant", "Done", tool_calls=(_tc("grep_files"),)),
        _m("tool", "10 matches"),
        _m("assistant", "Found 10 TODOs"),
        _m("user", "Task 2: write a report"),
        _m("assistant", "Writing", tool_calls=(_tc("file_write"),)),
        _m("tool", "ok wrote 1.2 KB"),
        _m("assistant", "Report written to /tmp/todos.md"),
    ]
    out = h._build_compression_summary_rule_based(msgs, "")
    # Both subgoals present
    assert "Subgoal 1" in out
    assert "Subgoal 2" in out
    assert "Task 1: find TODOs" in out
    assert "Task 2: write a report" in out
    # Tool tallies per subgoal
    assert out.count("1 tool call(s)") == 2
    # Tools named
    assert "grep_files" in out
    assert "file_write" in out
    # Synthesis text per subgoal
    assert "Found 10 TODOs" in out
    assert "Report written" in out


def test_summary_roles_line_at_bottom():
    """For parity with downstream consumers that grep the old digest's
    ``user: N message(s)`` line."""
    h = _StubMixin()
    msgs = [
        _m("user", "u1"), _m("assistant", "a1"),
        _m("user", "u2"), _m("assistant", "a2"),
    ]
    out = h._build_compression_summary_rule_based(msgs, "")
    assert "Roles:" in out
    assert "user: 2" in out
    assert "assistant: 2" in out


def test_summary_appends_provider_extract():
    h = _StubMixin()
    msgs = [_m("user", "u"), _m("assistant", "a")]
    out = h._build_compression_summary_rule_based(
        msgs, "User prefers terse responses.",
    )
    assert "Memory-extracted facts" in out
    assert "terse responses" in out


def test_summary_compresses_many_tools_into_one_line():
    """When a subgoal has 20 tool calls, the count is reported but each
    isn't listed verbatim — only first 6 unique names shown."""
    h = _StubMixin()
    msgs = [_m("user", "task")]
    msgs.append(_m("assistant", "running many tools",
                   tool_calls=tuple(
                       _tc(f"tool_{i}") for i in range(20)
                   )))
    out = h._build_compression_summary_rule_based(msgs, "")
    assert "20 tool call(s)" in out
    # Earliest 6 visible
    assert "tool_0" in out
    assert "tool_5" in out
    # "more" footer captures the rest
    assert "+14 more" in out


def test_summary_failed_tool_summary():
    h = _StubMixin()
    msgs = [
        _m("user", "Try to read protected file"),
        _m("assistant", "trying", tool_calls=(_tc("file_read"),)),
        _m("tool", "FileNotFoundError: /protected/x"),
    ]
    out = h._build_compression_summary_rule_based(msgs, "")
    assert "1 failed" in out
