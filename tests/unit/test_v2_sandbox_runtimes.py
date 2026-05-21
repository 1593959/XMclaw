"""Unit tests for Modal, Daytona, Vercel, and Singularity runtimes.

All tests are fully mocked — no real network calls or container executions.
"""
from __future__ import annotations

import asyncio
import json
import os
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xmclaw.providers.runtime.daytona import DaytonaSkillRuntime
from xmclaw.providers.runtime.modal import ModalSkillRuntime
from xmclaw.providers.runtime.singularity import SingularitySkillRuntime
from xmclaw.providers.runtime.vercel import VercelSkillRuntime
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest


class _FakeSkill(Skill):
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


# ── Modal ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_modal_enforce_manifest_rejects_negative_cpu() -> None:
    rt = ModalSkillRuntime(token_id="t", token_secret="s")
    bad = _make_manifest(max_cpu_seconds=-5.0)
    with pytest.raises(ValueError, match="cpu"):
        await rt.fork(_make_skill(), bad, {})


@pytest.mark.asyncio
async def test_modal_fork_identity_mismatch_raises() -> None:
    rt = ModalSkillRuntime(token_id="t", token_secret="s")
    with pytest.raises(ValueError, match="identity mismatch"):
        await rt.fork(_make_skill(), _make_manifest(id="other.skill"), {})


@pytest.mark.asyncio
async def test_modal_missing_dependency_surfaces_structured_error() -> None:
    rt = ModalSkillRuntime(token_id="t", token_secret="s")
    with patch.object(
        rt,
        "_run_remote",
        return_value=SkillOutput(
            ok=False,
            result={"error": "modal not installed", "kind": "missing_dependency"},
            side_effects=[],
        ),
    ):
        handle = await rt.fork(_make_skill(), _make_manifest(), {})
        out = await rt.wait(handle)
    assert out.ok is False
    assert "modal" in str(out.result.get("error", "")).lower()


def test_modal_pack_skill_creates_zip() -> None:
    rt = ModalSkillRuntime(token_id="t", token_secret="s")
    tmp = Path(os.getcwd()) / ".test_modal_pack"
    tmp.mkdir(exist_ok=True)
    payload = rt._pack_skill(_make_skill(tmp), _make_manifest())
    assert payload.read_bytes()[:4] == b"PK\x03\x04"
    with zipfile.ZipFile(payload, "r") as zf:
        assert "_manifest.json" in zf.namelist()


@pytest.mark.asyncio
async def test_modal_status_unknown_handle_raises() -> None:
    rt = ModalSkillRuntime(token_id="t", token_secret="s")
    from xmclaw.providers.runtime.base import SkillHandle
    with pytest.raises(LookupError):
        await rt.status(SkillHandle(id="nope", skill_id="x", version=1))


# ── Daytona ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_daytona_enforce_manifest_rejects_negative_cpu() -> None:
    rt = DaytonaSkillRuntime(api_key="k", server_url="https://daytona.io")
    bad = _make_manifest(max_cpu_seconds=-5.0)
    with pytest.raises(ValueError, match="cpu"):
        await rt.fork(_make_skill(), bad, {})


@pytest.mark.asyncio
async def test_daytona_fork_identity_mismatch_raises() -> None:
    rt = DaytonaSkillRuntime(api_key="k", server_url="https://daytona.io")
    with pytest.raises(ValueError, match="identity mismatch"):
        await rt.fork(_make_skill(), _make_manifest(id="other.skill"), {})


@pytest.mark.asyncio
async def test_daytona_status_unknown_handle_raises() -> None:
    rt = DaytonaSkillRuntime(api_key="k", server_url="https://daytona.io")
    from xmclaw.providers.runtime.base import SkillHandle
    with pytest.raises(LookupError):
        await rt.status(SkillHandle(id="nope", skill_id="x", version=1))


# ── Vercel ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_vercel_enforce_manifest_rejects_negative_cpu() -> None:
    rt = VercelSkillRuntime(endpoint_url="https://example.com/api/skill")
    bad = _make_manifest(max_cpu_seconds=-5.0)
    with pytest.raises(ValueError, match="cpu"):
        await rt.fork(_make_skill(), bad, {})


@pytest.mark.asyncio
async def test_vercel_fork_identity_mismatch_raises() -> None:
    rt = VercelSkillRuntime(endpoint_url="https://example.com/api/skill")
    with pytest.raises(ValueError, match="identity mismatch"):
        await rt.fork(_make_skill(), _make_manifest(id="other.skill"), {})


@pytest.mark.asyncio
async def test_vercel_invokes_endpoint_and_parses_ok() -> None:
    rt = VercelSkillRuntime(
        endpoint_url="https://example.com/api/skill",
        auth_token="secret",
    )
    skill = _make_skill()
    manifest = _make_manifest()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"tag": "ok", "output": {"answer": 42}}
    mock_resp.text = json.dumps(mock_resp.json.return_value)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_cm):
        handle = await rt.fork(skill, manifest, {"q": "hello"})
        out = await rt.wait(handle)

    assert out.ok is True
    assert out.result == {"answer": 42}
    mock_client.post.assert_awaited_once()
    call_args = mock_client.post.await_args
    assert call_args[1]["headers"]["Authorization"] == "Bearer secret"
    assert call_args[1]["json"]["args"] == {"q": "hello"}


@pytest.mark.asyncio
async def test_vercel_http_error_surfaces_structured_error() -> None:
    rt = VercelSkillRuntime(endpoint_url="https://example.com/api/skill")
    skill = _make_skill()
    manifest = _make_manifest()

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_cm):
        handle = await rt.fork(skill, manifest, {})
        out = await rt.wait(handle)

    assert out.ok is False
    assert "500" in str(out.result.get("error", ""))


@pytest.mark.asyncio
async def test_vercel_status_unknown_handle_raises() -> None:
    rt = VercelSkillRuntime(endpoint_url="https://example.com/api/skill")
    from xmclaw.providers.runtime.base import SkillHandle
    with pytest.raises(LookupError):
        await rt.status(SkillHandle(id="nope", skill_id="x", version=1))


# ── Singularity ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_singularity_enforce_manifest_rejects_negative_cpu() -> None:
    rt = SingularitySkillRuntime()
    bad = _make_manifest(max_cpu_seconds=-5.0)
    with pytest.raises(ValueError, match="cpu"):
        await rt.fork(_make_skill(), bad, {})


@pytest.mark.asyncio
async def test_singularity_fork_identity_mismatch_raises() -> None:
    rt = SingularitySkillRuntime()
    with pytest.raises(ValueError, match="identity mismatch"):
        await rt.fork(_make_skill(), _make_manifest(id="other.skill"), {})


@pytest.mark.asyncio
async def test_singularity_detects_missing_cli() -> None:
    rt = SingularitySkillRuntime(singularity_cmd="nonexistent_singularity_xyz")
    with pytest.raises(RuntimeError, match="singularity"):
        await rt.fork(_make_skill(), _make_manifest(), {})


@pytest.mark.asyncio
async def test_singularity_status_unknown_handle_raises() -> None:
    rt = SingularitySkillRuntime()
    from xmclaw.providers.runtime.base import SkillHandle
    with pytest.raises(LookupError):
        await rt.status(SkillHandle(id="nope", skill_id="x", version=1))
