"""Ground-truth checks: ran / returned / type_matched / side_effect_observable.

Each check inspects a ``BehavioralEvent`` and returns (bool, evidence_list).
These are the functions that replace LLM-as-judge (anti-requirement #4).

Phase 1 targets ``tool_invocation_finished`` events. Skill-level checks land
once the SkillRuntime is wired up (Phase 3).
"""
from __future__ import annotations

from xmclaw.core.bus.events import BehavioralEvent


def check_ran(event: BehavioralEvent) -> tuple[bool, list[str]]:
    """Did the tool actually execute, or did the model just talk about it?"""
    raise NotImplementedError("Phase 1")


def check_returned(event: BehavioralEvent) -> tuple[bool, list[str]]:
    """Did the tool produce a return value (not an exception, not empty)?"""
    raise NotImplementedError("Phase 1")


def check_type_matched(event: BehavioralEvent) -> tuple[bool, list[str]]:
    """Does the return value match the tool's declared output schema?"""
    raise NotImplementedError("Phase 1")


def check_side_effect_observable(
    event: BehavioralEvent,
) -> tuple[bool | None, list[str]]:
    """For tools that should mutate state: is the mutation verifiable post-hoc?

    Returns ``None`` if the tool is pure (no side effect expected).
    """
    raise NotImplementedError("Phase 1")
