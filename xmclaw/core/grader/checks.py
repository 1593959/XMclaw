"""Ground-truth checks: ran / returned / type_matched / side_effect_observable.

Each check inspects a ``BehavioralEvent`` and returns ``(value, evidence)``.
These are the functions that replace LLM-as-judge (anti-requirement #4).

Conventions:
- Value: ``True`` / ``False`` — or ``None`` for ``check_side_effect_observable``
  when the tool declared no expected side effects (not applicable).
- Evidence: a list of short strings — each a replay-able pointer (call_id,
  declared type, file path, etc.). MUST be non-empty whenever the check
  returns ``True`` — we never pass-on-trust.
"""
from __future__ import annotations

import os
from typing import Any

from xmclaw.core.bus.events import BehavioralEvent, EventType

_TYPE_NAMES: dict[str, tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "float": (float, int),  # Python int ⊂ float for type-match purposes
    "bool": (bool,),
    "dict": (dict,),
    "list": (list, tuple),
    "none": (type(None),),
}


def _preview(value: Any, limit: int = 40) -> str:  # noqa: ANN401
    s = repr(value)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def check_ran(event: BehavioralEvent) -> tuple[bool, list[str]]:
    """Did the tool actually execute?

    Evidence of execution = the event itself is a ``tool_invocation_finished``
    with a ``call_id``. An ``anti_req_violation`` event (e.g. LLM emitted text
    that *described* a tool call) counts as ran=False.
    """
    if event.type == EventType.TOOL_INVOCATION_FINISHED:
        call_id = event.payload.get("call_id")
        if call_id:
            return True, [f"call_id={call_id}"]
        return False, ["finished event missing call_id"]
    if event.type == EventType.ANTI_REQ_VIOLATION:
        return False, [f"anti_req_violation: {event.payload.get('message', '?')}"]
    return False, [f"unexpected event type: {event.type.value}"]


def check_returned(event: BehavioralEvent) -> tuple[bool, list[str]]:
    """Did the tool produce a return value (not an error, not None)?"""
    error = event.payload.get("error")
    if error:
        return False, [f"error={error!r}"]
    if "result" not in event.payload:
        return False, ["payload missing 'result' key"]
    result = event.payload["result"]
    if result is None:
        return False, ["result is None"]
    return True, [f"result={_preview(result)}"]


def check_type_matched(event: BehavioralEvent) -> tuple[bool, list[str]]:
    """Does the return value match the tool's declared output type?

    When ``expected_type`` is absent, returns ``True`` with evidence that no
    type was declared (so this check is not discriminating — don't give it
    full weight in that case).
    """
    if "result" not in event.payload or event.payload["result"] is None:
        return False, ["no result to type-check"]
    expected = event.payload.get("expected_type")
    if not expected:
        return True, ["no expected_type declared"]
    result = event.payload["result"]
    canonical = _TYPE_NAMES.get(expected.lower())
    if canonical is None:
        return False, [f"unknown expected_type={expected!r}"]
    if isinstance(result, canonical):
        return True, [f"expected={expected} got={type(result).__name__}"]
    return False, [f"expected={expected} got={type(result).__name__}"]


def check_side_effect_observable(
    event: BehavioralEvent,
) -> tuple[bool | None, list[str]]:
    """For tools that should mutate state: is the mutation verifiable post-hoc?

    Returns:
      ``None`` if the tool declared no expected side effects (not applicable).
      ``True`` if every declared side-effect path/URI exists and is inspectable.
      ``False`` if any declared side effect is missing.

    Phase 1 only verifies filesystem paths. URIs (http://, redis://, etc.) will
    get per-scheme verifiers in Phase 2.
    """
    declared: list[str] = event.payload.get("expected_side_effects") or []
    if not declared:
        return None, ["no side effects declared"]

    missing: list[str] = []
    present: list[str] = []
    for s in declared:
        if "://" in s:
            # Non-filesystem side-effect; Phase 2 plugs in verifiers.
            present.append(f"unchecked (non-fs): {s}")
            continue
        if os.path.exists(s):
            present.append(f"exists: {s}")
        else:
            missing.append(f"missing: {s}")

    if missing:
        return False, missing + present
    return True, present
