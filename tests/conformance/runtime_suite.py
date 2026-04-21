"""SkillRuntime conformance suite (anti-req #10).

Every ``SkillRuntime`` (local/docker/ssh/modal/...) must pass the same
behavioral tests. Phase 3 deliverable.
"""
from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Phase 3")
def test_fork_exec_kill() -> None:
    raise NotImplementedError


@pytest.mark.skip(reason="Phase 3")
def test_manifest_blocks_unauthorized_fs_access() -> None:
    raise NotImplementedError
