"""B-398: break the CognitiveDaemon percept self-reaction loop.

User observed a "react_to_ws_user_…" task spinning for 1000+ seconds.
Root cause: every run_turn pushed a percept onto the PerceptionBus,
INCLUDING the internal turns the CognitiveDaemon itself spawned to
react to percepts. So: user msg → percept → daemon reacts → internal
turn → percept → daemon reacts → … forever.

The fix tags internal-session turns and skips the percept push for
them. This test pins the classifier so a future session-naming change
doesn't silently reopen the loop.
"""
from __future__ import annotations

import pytest

from xmclaw.daemon.agent_loop import _is_internal_session


# ─── internal sessions (must skip percept push) ───────────────────


@pytest.mark.parametrize("sid", [
    "autonomous:plan_step_3:a1b2c3d4",
    "autonomous:abc",
    "goal-from-percept-percept_xyz",
    "goal-from-percept-<no-id>",
    "reflect:nightly",
    "_system:cognitive_daemon",
    "_system:skill_proposer",
    "main:to:worker_1",          # agent-to-agent delegation
    "agentA:to:agentB:session",
])
def test_internal_sessions_detected(sid):
    assert _is_internal_session(sid) is True, (
        f"{sid!r} is an internal session — its turn must NOT push a "
        f"percept (would re-trigger the cognitive daemon)."
    )


# ─── real user sessions (must push percept normally) ──────────────


@pytest.mark.parametrize("sid", [
    "chat-9987e40d",
    "default",
    "openai-abc123",
    "feishu-ou_xxx",
    "main",                       # the primary chat session
    "shop",                       # a user-named session
])
def test_real_user_sessions_not_flagged(sid):
    assert _is_internal_session(sid) is False, (
        f"{sid!r} is a real user session — its turn SHOULD push a "
        f"percept so the cognitive daemon can observe genuine input."
    )


# ─── edge cases ───────────────────────────────────────────────────


def test_empty_session_is_not_internal():
    assert _is_internal_session("") is False
    assert _is_internal_session(None) is False  # type: ignore[arg-type]


def test_substring_not_prefix_does_not_false_positive():
    """A real session whose name merely CONTAINS 'autonomous' but
    doesn't start with the marker prefix should not be misclassified."""
    assert _is_internal_session("my-autonomous-notes") is False
    assert _is_internal_session("reflecting-on-x") is False
