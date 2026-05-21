"""Unit tests for SSHSkillRuntime (sandbox multi-backend #2).

These tests verify the non-SSH-dependent paths: manifest enforcement, skill
packing, identity mismatch, missing-paramiko error surfacing, and the
in-memory task map.  They do NOT open real network connections.
"""
from __future__ import annotations

import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from xmclaw.providers.runtime.ssh import SSHSkillRuntime
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest


class _FakeSkill(Skill):
    """Minimal Skill duck for runtime tests."""

    def __init__(self, skill_id: str, version: int, source_dir: Path | None = None) -> None:
        self.id = skill_id
        self.version = version
        self.source_dir = source_dir or Path(__file__).parent

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        return SkillOutput(ok=True, result="ok", side_effects=[])


def _make_manifest(**kwargs: object) -> SkillManifest:
    defaults: dict[str, object] = {
        "id": "test.skill",
        "version": 1,
        "title": "Test",
        "description": "D",
        "max_cpu_seconds": 10.0,
        "max_memory_mb": 128,
    }
    defaults.update(kwargs)
    return SkillManifest(**defaults)  # type: ignore[arg-type]


def _make_skill(path: Path | None = None) -> Skill:
    return _FakeSkill(skill_id="test.skill", version=1, source_dir=path)


# ── Manifest enforcement ──
@pytest.mark.asyncio
async def test_enforce_manifest_rejects_negative_cpu() -> None:
    rt = SSHSkillRuntime(host="h", user="u")
    bad = _make_manifest(max_cpu_seconds=-5.0)
    with pytest.raises(ValueError, match="cpu"):
        await rt.fork(
            _make_skill(),
            bad,
            {},
        )


@pytest.mark.asyncio
async def test_fork_identity_mismatch_raises() -> None:
    rt = SSHSkillRuntime(host="h", user="u")
    skill = _make_skill()
    manifest = _make_manifest(id="other.skill")
    with pytest.raises(ValueError, match="identity mismatch"):
        await rt.fork(skill, manifest, {})


# ── Missing dependency surfacing ──
@pytest.mark.asyncio
async def test_missing_paramiko_surfaces_structured_error() -> None:
    rt = SSHSkillRuntime(host="h", user="u")
    skill = _make_skill()
    manifest = _make_manifest()
    # Simulate what happens when paramiko is missing: _run_remote catches
    # the ImportError and returns a structured SkillOutput.
    with patch.object(
        rt,
        "_run_remote",
        return_value=SkillOutput(
            ok=False,
            result={
                "error": "SSHSkillRuntime needs the 'paramiko' package.",
                "kind": "missing_dependency",
            },
            side_effects=[],
        ),
    ):
        handle = await rt.fork(skill, manifest, {})
        out = await rt.wait(handle)
    assert out.ok is False
    assert "paramiko" in str(out.result.get("error", "")).lower()


# ── Skill packing ──
def test_pack_skill_creates_zip() -> None:
    rt = SSHSkillRuntime(host="h", user="u")
    tmp = Path(os.getcwd()) / ".test_pack"
    tmp.mkdir(exist_ok=True)
    skill = _make_skill(tmp)
    manifest = _make_manifest()

    payload = rt._pack_skill(skill, manifest)
    assert payload.read_bytes()[:4] == b"PK\x03\x04"

    # Verify zip contains at least the manifest entry
    with zipfile.ZipFile(payload, "r") as zf:
        names = zf.namelist()
        assert "_manifest.json" in names


# ── Task lifecycle (mocked SSH) ──
@pytest.mark.asyncio
async def test_kill_sets_killed_flag() -> None:
    rt = SSHSkillRuntime(host="h", user="u")
    skill = _make_skill()
    manifest = _make_manifest()

    # Mock _run_remote to block so the task stays alive long enough to kill.
    with patch.object(rt, "_run_remote", side_effect=lambda *a, **k: __import__("time").sleep(3600)):
        handle = await rt.fork(skill, manifest, {})

    slot = rt._slots[handle.id]
    assert not slot.task.done()

    # kill() should mark the slot as killed without blocking on the thread.
    await rt.kill(handle)
    assert slot.killed is True

    # Cleanup: remove the slot so subsequent tests aren't affected.
    del rt._slots[handle.id]


@pytest.mark.asyncio
async def test_status_unknown_handle_raises() -> None:
    rt = SSHSkillRuntime(host="h", user="u")
    from xmclaw.providers.runtime.base import SkillHandle

    with pytest.raises(LookupError):
        await rt.status(SkillHandle(id="nope", skill_id="x", version=1))
