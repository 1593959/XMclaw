"""Module-top-level demo skills — importable by ``multiprocessing`` spawn.

Top-level classes with simple ``__init__`` args are picklable and can
be materialised in a subprocess by the ``ProcessSkillRuntime``. Test-
helper classes nested inside a pytest file CAN'T be pickled across a
spawn boundary (the child imports your test module and the class
isn't findable at its original path).

Keep these skills intentionally trivial — they're for runtime
conformance + ProcessSkillRuntime unit tests, not for end-user logic.
"""
from __future__ import annotations

import asyncio

from xmclaw.skills.base import Skill, SkillInput, SkillOutput


class PickleEcho(Skill):
    """Echoes its input args — basic smoke-test skill."""

    id = "demo.pickle_echo"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:
        return SkillOutput(
            ok=True, result={"echoed": inp.args}, side_effects=[],
        )


class PickleSlow(Skill):
    """Sleeps for ``duration`` seconds — used to exercise timeout / kill."""

    id = "demo.pickle_slow"
    version = 1

    def __init__(self, duration: float) -> None:
        self._duration = duration

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        await asyncio.sleep(self._duration)
        return SkillOutput(
            ok=True, result={"slept": self._duration}, side_effects=[],
        )


class PickleRaising(Skill):
    """Raises on run — tests the skill-error envelope."""

    id = "demo.pickle_raising"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        raise RuntimeError("intentional failure from picklable skill")


class PickleCheckCwd(Skill):
    """Returns the current working directory — tests fs sandbox."""

    id = "demo.pickle_check_cwd"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        import os
        return SkillOutput(
            ok=True, result={"cwd": os.getcwd()}, side_effects=[],
        )


class PickleSubprocess(Skill):
    """Runs a subprocess command — tests subprocess guard."""

    id = "demo.pickle_subprocess"
    version = 1

    def __init__(self, cmd: list[str]) -> None:
        self._cmd = cmd

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        import subprocess
        try:
            result = subprocess.run(
                self._cmd, capture_output=True, text=True, timeout=5,
            )
            return SkillOutput(
                ok=True,
                result={
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                },
                side_effects=[],
            )
        except PermissionError as exc:
            return SkillOutput(
                ok=False,
                result={"error": str(exc), "kind": "permission_denied"},
                side_effects=[],
            )


class PickleMemoryHog(Skill):
    """Allocates a large bytearray and holds it — tests memory guard.

    Bytearray is denser than ``list(range(n))`` (1 byte per element vs
    ~28 bytes per int object) so we can exceed a modest limit without
    needing tens of millions of objects.
    """

    id = "demo.pickle_memory_hog"
    version = 1

    def __init__(self, n_bytes: int, hold_seconds: float = 2.0) -> None:
        self._n_bytes = n_bytes
        self._hold_seconds = hold_seconds

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        big = bytearray(self._n_bytes)
        await asyncio.sleep(self._hold_seconds)
        return SkillOutput(
            ok=True, result={"allocated": len(big)}, side_effects=[],
        )
