"""SkillRuntime interface — where skills actually execute.

Phase 1: not used (demo runs in-process). Phase 3 brings
fork/exec/kill + manifest-driven sandbox.
"""
from xmclaw.providers.runtime.base import SkillHandle, SkillRuntime, SkillStatus

__all__ = ["SkillHandle", "SkillRuntime", "SkillStatus"]
