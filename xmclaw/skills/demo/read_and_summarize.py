"""Demo skill — read a file, summarize it. Phase 1 go/no-go subject.

The bench runs this skill 50 times across varied files; the OnlineScheduler
must improve grader scores monotonically. See V2_DEVELOPMENT.md §8.2.
"""
from __future__ import annotations

from xmclaw.skills.base import Skill, SkillInput, SkillOutput


class ReadAndSummarize(Skill):
    id = "demo.read_and_summarize"
    version = 1

    async def run(self, inp: SkillInput) -> SkillOutput:  # noqa: ARG002
        raise NotImplementedError("Phase 1")
