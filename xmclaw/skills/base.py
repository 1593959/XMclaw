"""Skill ABC."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SkillInput:
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SkillOutput:
    ok: bool
    result: Any
    side_effects: list[str]


class Skill(abc.ABC):
    id: str
    version: int

    @abc.abstractmethod
    async def run(self, inp: SkillInput) -> SkillOutput: ...
