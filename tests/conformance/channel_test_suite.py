"""Channel conformance suite (CI-3, anti-req #7).

Every concrete ``ChannelAdapter`` must pass all 13 tests here before it
ships. Each test runs against a registered adapter in the matrix.

Phase 2 deliverable.
"""
from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Phase 2 — no channel adapters implemented yet")
def test_send_receive_roundtrip() -> None:
    raise NotImplementedError


@pytest.mark.skip(reason="Phase 2")
def test_reconnect_after_drop() -> None:
    raise NotImplementedError


@pytest.mark.skip(reason="Phase 2")
def test_rate_limit_handling() -> None:
    raise NotImplementedError


# ... (10 more tests listed in V2_DEVELOPMENT.md §3.3)
