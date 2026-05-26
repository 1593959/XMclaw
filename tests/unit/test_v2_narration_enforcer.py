"""Tests for the extracted NarrationEnforcer (audit G1 phase 2).

Locks the behavior previously inlined in ``hop_loop._run_hop_loop``:
* silent hops (tool_calls present, no plain text) accumulate
* 2 consecutive silent → soft nudge fires
* 3 consecutive silent → also publishes progress marker
* a visible-text hop resets the counter
"""
from __future__ import annotations

from xmclaw.daemon.narration_enforcer import NarrationEnforcer


def test_visible_text_keeps_counter_at_zero() -> None:
    n = NarrationEnforcer()
    d = n.observe_hop(
        response_content="working on it",
        has_tool_calls=True, hop=0,
    )
    assert n.silent_hops == 0
    assert d.nudge_message is None
    assert d.progress_marker is None


def test_no_tool_calls_keeps_counter_at_zero() -> None:
    n = NarrationEnforcer()
    d = n.observe_hop(
        response_content="", has_tool_calls=False, hop=0,
    )
    assert n.silent_hops == 0
    assert d.nudge_message is None


def test_first_silent_hop_does_not_fire() -> None:
    n = NarrationEnforcer()
    d = n.observe_hop(
        response_content="", has_tool_calls=True, hop=0,
    )
    assert n.silent_hops == 1
    assert d.nudge_message is None


def test_second_silent_hop_fires_soft_nudge() -> None:
    n = NarrationEnforcer()
    n.observe_hop(response_content="", has_tool_calls=True, hop=0)
    d = n.observe_hop(
        response_content="", has_tool_calls=True, hop=1,
        tool_names=["bash", "file_read"],
    )
    assert n.silent_hops == 2
    assert d.nudge_message is not None
    assert "2" in d.nudge_message  # carries the silent-hop count
    assert d.progress_marker is None  # not at hard threshold yet


def test_third_silent_hop_also_publishes_marker() -> None:
    n = NarrationEnforcer()
    for h in range(2):
        n.observe_hop(response_content="", has_tool_calls=True, hop=h)
    d = n.observe_hop(
        response_content="", has_tool_calls=True, hop=2,
        tool_names=["bash", "memory_search"],
    )
    assert n.silent_hops == 3
    assert d.nudge_message is not None
    assert d.progress_marker is not None
    assert d.progress_marker["kind"] == "narration_enforcement"
    assert "bash" in d.progress_marker["content"]


def test_visible_hop_resets_counter() -> None:
    n = NarrationEnforcer()
    for h in range(3):
        n.observe_hop(response_content="", has_tool_calls=True, hop=h)
    assert n.silent_hops == 3
    n.observe_hop(
        response_content="ok, here's what I found",
        has_tool_calls=False, hop=3,
    )
    assert n.silent_hops == 0
