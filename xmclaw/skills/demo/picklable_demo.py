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
