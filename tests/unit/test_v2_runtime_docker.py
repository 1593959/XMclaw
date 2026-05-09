"""DockerSkillRuntime — unit tests.

Tests run WITHOUT docker installed by mocking ``docker.from_env()``
through the runtime's ``client=`` injection point. We never actually
pull / start a real container — that's an integration concern for a
later runtime-conformance lane.

What we cover here:
  * Lazy SDK import: a runtime built without ``client=`` and without
    ``docker`` on PYTHONPATH surfaces a clear RuntimeError at first
    ``fork``, NOT at module import.
  * Container is created with the secure-by-default constraints
    (network_mode=none, mem_limit, read_only=True, tmpfs /tmp,
    cap_drop=ALL, no-new-privileges, plus a read-only mount).
  * stdin/stdout flow: harness emits an envelope; runtime parses it.
  * Timeout enforcement: container.wait raises a Timeout-named
    exception → runtime returns SkillOutput(ok=False, kind="timeout").
  * OOM detection: ``OOMKilled=True`` → SkillOutput(kind="oom").
  * Skill error envelope: harness reports tag="skill_error" → runtime
    surfaces as ok=False / kind="skill_error".
  * Cleanup: container.remove(force=True) is called and the staged
    mount dir is gone after wait().
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from xmclaw.providers.runtime.docker import (
    DockerSkillRuntime,
    _parse_envelope,
)
from xmclaw.providers.runtime.base import SkillStatus
from xmclaw.skills.base import Skill, SkillInput, SkillOutput
from xmclaw.skills.manifest import SkillManifest


# ── helper skill ─────────────────────────────────────────────────────────


class _NoopSkill(Skill):
    """Skill that the harness would run inside the container — but our
    tests never actually launch a container, so this stays a stub."""

    id = "demo.docker_noop"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        return SkillOutput(ok=True, result={"in": "container"}, side_effects=[])


def _m(id_: str = "demo.docker_noop", v: int = 1, *, max_cpu: float = 30.0) -> SkillManifest:
    return SkillManifest(id=id_, version=v, max_cpu_seconds=max_cpu)


# ── mock client builder ──────────────────────────────────────────────────


def _make_envelope_stdout(ok: bool = True, result: dict | None = None) -> bytes:
    """Build container stdout that the runtime's parser will accept."""
    payload = {
        "tag": "ok",
        "output": {
            "ok": ok,
            "result": result if result is not None else {"in": "container"},
            "side_effects": [],
        },
    }
    return ("noise from skill imports\n"
            "__XMC_ENVELOPE__: " + json.dumps(payload) + "\n").encode("utf-8")


def _make_mock_client(
    *,
    stdout: bytes | None = None,
    wait_returns: dict | None = None,
    wait_raises: Exception | None = None,
    oom_killed: bool = False,
    image_present: bool = True,
) -> MagicMock:
    """Create a stand-in for ``docker.from_env()`` that records calls."""
    client = MagicMock(name="docker_client")

    # images.get / images.pull
    if image_present:
        client.images.get.return_value = MagicMock()
    else:
        client.images.get.side_effect = RuntimeError("ImageNotFound")
        client.images.pull.return_value = MagicMock()

    container = MagicMock(name="container")
    container.attrs = {"State": {"OOMKilled": oom_killed, "ExitCode": 0,
                                  "Status": "exited"}}
    if wait_raises is not None:
        container.wait.side_effect = wait_raises
    else:
        container.wait.return_value = wait_returns or {"StatusCode": 0}
    container.logs.return_value = stdout if stdout is not None else _make_envelope_stdout()

    client.containers.create.return_value = container
    return client


# ── lazy import ──────────────────────────────────────────────────────────


def test_lazy_import_surfaces_clear_error_when_docker_missing(monkeypatch):
    """The runtime constructor must NOT require docker — only fork() does.

    We simulate the missing-SDK scenario by removing the docker module
    from sys.modules and blocking the import. Importing
    ``xmclaw.providers.runtime.docker`` itself stays clean (it's already
    loaded above); the lazy ``_docker_module`` call is what should fail.
    """
    rt = DockerSkillRuntime()

    # Make ``import docker`` raise inside ``_docker_module``.
    real_modules = dict(sys.modules)
    sys.modules.pop("docker", None)

    class _Blocker:
        def find_module(self, name, path=None):  # noqa: D401 — meta-finder API
            if name == "docker" or name.startswith("docker."):
                return self
            return None

        def load_module(self, name):
            raise ImportError(f"blocked test import of {name}")

    blocker = _Blocker()
    sys.meta_path.insert(0, blocker)
    try:
        with pytest.raises(RuntimeError, match="docker.*Python SDK"):
            rt._docker_module()
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.update(real_modules)


def test_module_import_does_not_require_docker():
    """Importing the runtime module on a docker-less install must work.

    The actual ``import xmclaw.providers.runtime.docker`` already
    succeeded for these tests to load at all; this test is a regression
    guard against someone moving the lazy import to module top.
    """
    import xmclaw.providers.runtime.docker as mod

    assert mod.DockerSkillRuntime is not None
    # Module must NOT have a top-level ``docker`` attribute that would
    # indicate an eager import — only the lazy helper.
    assert not hasattr(mod, "docker")


# ── enforce_manifest ─────────────────────────────────────────────────────


def test_enforce_manifest_accepts_well_formed():
    DockerSkillRuntime().enforce_manifest(_m())


def test_enforce_manifest_rejects_negative_cpu():
    rt = DockerSkillRuntime()
    with pytest.raises(ValueError, match="max_cpu_seconds"):
        rt.enforce_manifest(SkillManifest(id="x", version=1, max_cpu_seconds=-1))


def test_enforce_manifest_rejects_negative_memory():
    rt = DockerSkillRuntime()
    with pytest.raises(ValueError, match="max_memory_mb"):
        rt.enforce_manifest(SkillManifest(id="x", version=1, max_memory_mb=-1))


# ── fork: container is created with the secure defaults ──────────────────


@pytest.mark.asyncio
async def test_fork_creates_container_with_secure_defaults(tmp_path):
    client = _make_mock_client()
    rt = DockerSkillRuntime(client=client)

    handle = await rt.fork(_NoopSkill(), _m(), args={"x": 1})

    # One container created, with our hardened constraints.
    assert client.containers.create.call_count == 1
    kwargs = client.containers.create.call_args.kwargs
    assert kwargs["image"] == "python:3.10-slim"
    assert kwargs["network_mode"] == "none"
    assert kwargs["mem_limit"] == "512m"
    assert kwargs["cpu_quota"] == 50000
    assert kwargs["cpu_period"] == 100000
    assert kwargs["read_only"] is True
    assert kwargs["tmpfs"] == {"/tmp": "size=100M"}
    assert kwargs["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in kwargs["security_opt"]
    # /skill is mounted read-only.
    volumes = kwargs["volumes"]
    assert len(volumes) == 1
    mount_options = next(iter(volumes.values()))
    assert mount_options["bind"] == "/skill"
    assert mount_options["mode"] == "ro"
    # Container is started after creation (detach=True so it returns
    # immediately).
    container = client.containers.create.return_value
    container.start.assert_called_once()
    # Handle gets a real id; pid is None for docker.
    assert handle.id
    assert handle.pid is None


@pytest.mark.asyncio
async def test_fork_writes_skill_meta_and_input_into_mount_dir():
    """Harness reads /skill/skill_meta.json + /skill/input.json — they
    must land in the staged mount dir before the container starts."""
    client = _make_mock_client()
    rt = DockerSkillRuntime(client=client)

    await rt.fork(_NoopSkill(), _m(), args={"hello": "world"})

    kwargs = client.containers.create.call_args.kwargs
    mount_dir = Path(next(iter(kwargs["volumes"])))
    # The runtime cleans up only on wait()/kill(), so files exist now.
    assert (mount_dir / "skill_meta.json").exists()
    assert (mount_dir / "input.json").exists()
    assert (mount_dir / "harness.py").exists()
    meta = json.loads((mount_dir / "skill_meta.json").read_text())
    assert meta["id"] == "demo.docker_noop"
    assert meta["version"] == 1
    assert meta["qualname"] == "_NoopSkill"
    inp = json.loads((mount_dir / "input.json").read_text())
    assert inp == {"args": {"hello": "world"}}


@pytest.mark.asyncio
async def test_fork_pulls_image_when_missing():
    client = _make_mock_client(image_present=False)
    rt = DockerSkillRuntime(client=client)

    await rt.fork(_NoopSkill(), _m(), args={})

    client.images.get.assert_called_once_with("python:3.10-slim")
    client.images.pull.assert_called_once_with("python:3.10-slim")


@pytest.mark.asyncio
async def test_fork_skips_pull_when_image_cached():
    client = _make_mock_client(image_present=True)
    rt = DockerSkillRuntime(client=client)

    await rt.fork(_NoopSkill(), _m(), args={})
    await rt.fork(_NoopSkill(), _m(), args={})

    # Second fork must NOT re-check / re-pull because the runtime
    # memoizes the image set.
    assert client.images.get.call_count == 1
    client.images.pull.assert_not_called()


# ── wait: stdin/stdout envelope flow ─────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_parses_ok_envelope_to_skill_output():
    client = _make_mock_client(stdout=_make_envelope_stdout(
        ok=True, result={"answer": 42},
    ))
    rt = DockerSkillRuntime(client=client)

    handle = await rt.fork(_NoopSkill(), _m(), args={})
    out = await rt.wait(handle)

    assert out.ok is True
    assert out.result == {"answer": 42}
    assert await rt.status(handle) == SkillStatus.SUCCEEDED
    # Container must have been removed.
    container = client.containers.create.return_value
    container.remove.assert_called_with(force=True)


@pytest.mark.asyncio
async def test_wait_surfaces_skill_error_envelope():
    bad_envelope = ("__XMC_ENVELOPE__: "
                    + json.dumps({"tag": "skill_error",
                                   "error": "RuntimeError: boom"})
                    + "\n").encode("utf-8")
    client = _make_mock_client(
        stdout=bad_envelope,
        wait_returns={"StatusCode": 1},
    )
    rt = DockerSkillRuntime(client=client)

    handle = await rt.fork(_NoopSkill(), _m(), args={})
    out = await rt.wait(handle)

    assert out.ok is False
    assert "RuntimeError: boom" in out.result["error"]
    assert out.result["kind"] == "skill_error"
    assert await rt.status(handle) == SkillStatus.FAILED


@pytest.mark.asyncio
async def test_wait_handles_no_envelope_gracefully():
    client = _make_mock_client(stdout=b"some random stdout, no envelope\n")
    rt = DockerSkillRuntime(client=client)

    handle = await rt.fork(_NoopSkill(), _m(), args={})
    out = await rt.wait(handle)

    assert out.ok is False
    assert out.result["kind"] == "no_envelope"
    assert "container produced no envelope" in out.result["error"]


# ── timeout ──────────────────────────────────────────────────────────────


class _ReadTimeout(Exception):
    """Stand-in for ``requests.exceptions.ReadTimeout`` — the runtime
    detects timeouts by class-name suffix to avoid taking a hard
    dep on requests."""


@pytest.mark.asyncio
async def test_wait_translates_container_timeout_to_structured_failure():
    client = _make_mock_client(wait_raises=_ReadTimeout("deadline"))
    rt = DockerSkillRuntime(client=client, timeout_s=1.0)

    handle = await rt.fork(_NoopSkill(), _m(max_cpu=30.0), args={})
    out = await rt.wait(handle)

    assert out.ok is False
    assert out.result["kind"] == "timeout"
    assert "timeout" in out.result["error"].lower()
    assert await rt.status(handle) == SkillStatus.TIMEOUT
    # Timed-out container must be killed + cleaned up.
    container = client.containers.create.return_value
    container.kill.assert_called()
    container.remove.assert_called_with(force=True)


# ── OOM ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_detects_oom_killed():
    client = _make_mock_client(
        wait_returns={"StatusCode": 137},
        oom_killed=True,
        stdout=b"",  # OOM child usually writes nothing
    )
    rt = DockerSkillRuntime(client=client, mem_limit="64m")

    handle = await rt.fork(_NoopSkill(), _m(), args={})
    out = await rt.wait(handle)

    assert out.ok is False
    assert out.result["kind"] == "oom"
    assert "64m" in out.result["error"]
    assert await rt.status(handle) == SkillStatus.FAILED


# ── kill ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kill_terminates_container_and_marks_handle():
    client = _make_mock_client()
    rt = DockerSkillRuntime(client=client)

    handle = await rt.fork(_NoopSkill(), _m(), args={})
    await rt.kill(handle)

    container = client.containers.create.return_value
    container.kill.assert_called()
    assert await rt.status(handle) == SkillStatus.KILLED


@pytest.mark.asyncio
async def test_kill_idempotent_after_natural_completion():
    client = _make_mock_client()
    rt = DockerSkillRuntime(client=client)

    handle = await rt.fork(_NoopSkill(), _m(), args={})
    await rt.wait(handle)
    # Status was SUCCEEDED. kill() must not flip it to KILLED.
    await rt.kill(handle)
    assert await rt.status(handle) == SkillStatus.SUCCEEDED


# ── cleanup ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_cleans_up_mount_dir_on_success():
    client = _make_mock_client()
    rt = DockerSkillRuntime(client=client)

    handle = await rt.fork(_NoopSkill(), _m(), args={})
    kwargs = client.containers.create.call_args.kwargs
    mount_dir = Path(next(iter(kwargs["volumes"])))
    assert mount_dir.exists()

    await rt.wait(handle)

    # After wait, the staging dir is GC'd.
    assert not mount_dir.exists()


# ── envelope parser unit cases ───────────────────────────────────────────


def test_parse_envelope_returns_none_on_empty_stdout():
    assert _parse_envelope("") is None
    assert _parse_envelope("no marker here\n") is None


def test_parse_envelope_finds_last_marker_line():
    stdout = (
        "info: skill loaded\n"
        '__XMC_ENVELOPE__: {"tag": "ok", "output": {"ok": true, "result": 1, '
        '"side_effects": []}}\n'
        "trailing junk\n"
    )
    obj = _parse_envelope(stdout)
    assert obj is not None
    assert obj["tag"] == "ok"


def test_parse_envelope_rejects_malformed_json():
    stdout = "__XMC_ENVELOPE__: {not json}\n"
    assert _parse_envelope(stdout) is None


# ── factory wiring ───────────────────────────────────────────────────────


def test_factory_accepts_docker_backend_with_full_docker_section():
    """Smoke test for ``build_skill_runtime_from_config`` — the docker
    sub-section must round-trip into the runtime constructor."""
    from xmclaw.daemon.factory import build_skill_runtime_from_config

    cfg = {
        "runtime": {
            "backend": "docker",
            "docker": {
                "image": "alpine:3.18",
                "network_mode": "bridge",
                "mem_limit": "256m",
                "cpu_quota": 25000,
                "cpu_period": 100000,
                "read_only": False,
                "tmpfs": {"/scratch": "size=50M"},
                "timeout_s": 60,
            },
        },
    }
    rt = build_skill_runtime_from_config(cfg)

    assert isinstance(rt, DockerSkillRuntime)
    assert rt.image == "alpine:3.18"
    assert rt.network_mode == "bridge"
    assert rt.mem_limit == "256m"
    assert rt.cpu_quota == 25000
    assert rt.read_only is False
    assert rt.tmpfs == {"/scratch": "size=50M"}
    assert rt.timeout_s == 60.0


def test_factory_docker_backend_rejects_bad_field_type():
    from xmclaw.daemon.factory import (
        ConfigError,
        build_skill_runtime_from_config,
    )

    cfg = {
        "runtime": {
            "backend": "docker",
            "docker": {"mem_limit": 512},  # int, not str
        },
    }
    with pytest.raises(ConfigError, match="mem_limit"):
        build_skill_runtime_from_config(cfg)


def test_factory_docker_backend_uses_defaults_when_section_absent():
    from xmclaw.daemon.factory import build_skill_runtime_from_config

    rt = build_skill_runtime_from_config({"runtime": {"backend": "docker"}})

    assert isinstance(rt, DockerSkillRuntime)
    assert rt.image == "python:3.10-slim"
    assert rt.network_mode == "none"
    assert rt.read_only is True
