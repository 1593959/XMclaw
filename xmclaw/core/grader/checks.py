"""Ground-truth checks: ran / returned / type_matched / side_effect_observable.

Each check inspects a ``BehavioralEvent`` and returns ``(value, evidence)``.
These are the deterministic functions that replace LLM-as-judge
(anti-requirement #4) and form Signal A in Sprint 3's multi-signal
pipeline (Iron Rule #1).

Conventions:
- Value: ``True`` / ``False`` — or ``None`` to mean "check is not
  applicable to this event" (e.g. ``check_type_matched`` when no
  ``expected_type`` was declared, ``check_side_effect_observable``
  when no ``expected_side_effects`` were declared).
- Evidence: a list of short strings — each a replay-able pointer
  (call_id, declared type, file path, etc.). MUST be non-empty
  whenever the check returns ``True`` — we never pass-on-trust.

Sprint 3 tightening (Iron Rule #1):

* :func:`check_ran` — was previously trivially True for any
  ``TOOL_INVOCATION_FINISHED`` with a ``call_id``. The audit found
  this gives credit to "tool didn't crash" alone (70-80% of the
  prior verdict). Now ``ran`` ALSO requires non-trivial output:
  the result must be present, non-empty (after ``str().strip()``),
  and not a "fake-success" sentinel (``"ok"`` / ``"done"`` /
  ``"true"`` / ``"success"`` etc.). Crashed / silent tools no
  longer score "ran".
* :func:`check_type_matched` — was previously True with a caveat
  when ``expected_type`` was not declared. The grader (verdict.py)
  used to count that as a positive. Now the check returns ``None``
  when no type was declared so the grader's combiner skips the
  check entirely instead of awarding it.
* :func:`check_side_effect_observable` — extended beyond filesystem
  paths to also count: bus-event emissions (``bus://event-type``),
  memory-provider writes (``memory://...``), and tool-result-with-
  payload as a soft observable when no other side-effect was
  declared. URI verification is still scheme-by-scheme; non-fs
  schemes that aren't in the registry are returned as
  "unchecked-but-declared" rather than silently True.
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

# "Fake success" sentinels — terse output strings that look like the
# tool did something useful but carry zero information. The audit
# called these out because the prior `check_ran` happily counted them
# as a positive. Sprint 3 rejects them: a tool returning "ok" is
# returning evidence-of-execution, not evidence-of-usefulness.
_FAKE_SUCCESS_SENTINELS: frozenset[str] = frozenset({
    "ok",
    "okay",
    "done",
    "true",
    "yes",
    "success",
    "successful",
    "finished",
    "completed",
    "1",  # bare integer-as-string from naive coercion
})


def _preview(value: Any, limit: int = 40) -> str:  # noqa: ANN401
    s = repr(value)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _is_trivial_output(result: Any) -> tuple[bool, str]:  # noqa: ANN401
    """Return ``(is_trivial, reason)`` for "fake success" detection.

    Trivial = empty / whitespace-only / a known fake-success sentinel.
    Non-string types (dict, list, int) are never trivial here — those
    carry structural payload regardless of size.
    """
    if result is None:
        return True, "result is None"
    if isinstance(result, str):
        stripped = result.strip()
        if not stripped:
            return True, "result is empty / whitespace-only"
        if stripped.lower() in _FAKE_SUCCESS_SENTINELS:
            return True, f"result is fake-success sentinel: {stripped!r}"
        return False, ""
    if isinstance(result, (bytes, bytearray)):
        # Empty bytes = trivial. Anything else is structural.
        return (not result), ("result is empty bytes" if not result else "")
    if isinstance(result, (list, tuple, set, dict)):
        # Empty container = trivial. Anything with even one item is
        # structural — let downstream checks decide if it's "good".
        return (not result), (
            f"result is empty {type(result).__name__}" if not result else ""
        )
    # int / float / bool / other — all non-trivial as long as not None.
    return False, ""


def check_ran(event: BehavioralEvent) -> tuple[bool, list[str]]:
    """Did the tool actually execute AND produce non-trivial output?

    Sprint 3 tightening: pre-Sprint-3 this was True for any
    ``tool_invocation_finished`` with a ``call_id``. That made
    ``ran=True`` trivially common (70-80% of every grader verdict
    came from this path) and let "fake success" tools get scored as
    successes. Now we require ALL of:

      1. event type is ``TOOL_INVOCATION_FINISHED``,
      2. ``call_id`` is present (provenance),
      3. ``result`` is present, non-trivial (see :func:`_is_trivial_output`),
      4. ``error`` is not set (an errored tool is not "ran successfully").

    An ``ANTI_REQ_VIOLATION`` event still counts as ran=False — the
    LLM emitted text that *described* a tool call but no tool ran.
    """
    if event.type == EventType.ANTI_REQ_VIOLATION:
        return False, [f"anti_req_violation: {event.payload.get('message', '?')}"]
    if event.type != EventType.TOOL_INVOCATION_FINISHED:
        return False, [f"unexpected event type: {event.type.value}"]

    call_id = event.payload.get("call_id")
    if not call_id:
        return False, ["finished event missing call_id"]

    error = event.payload.get("error")
    if error:
        return False, [f"call_id={call_id}", f"errored: {error!r}"]

    if "result" not in event.payload:
        return False, [f"call_id={call_id}", "no result key in payload"]

    result = event.payload["result"]
    is_trivial, reason = _is_trivial_output(result)
    if is_trivial:
        return False, [f"call_id={call_id}", f"non-trivial check failed: {reason}"]

    return True, [
        f"call_id={call_id}",
        f"result_preview={_preview(result)}",
    ]


def check_returned(event: BehavioralEvent) -> tuple[bool, list[str]]:
    """Did the tool produce a return value (not an error, not None)?

    This is the "raw structural" check — looser than :func:`check_ran`
    (which adds non-triviality). Kept distinct because callers
    sometimes want the looser shape independently.
    """
    error = event.payload.get("error")
    if error:
        return False, [f"error={error!r}"]
    if "result" not in event.payload:
        return False, ["payload missing 'result' key"]
    result = event.payload["result"]
    if result is None:
        return False, ["result is None"]
    return True, [f"result={_preview(result)}"]


def check_type_matched(
    event: BehavioralEvent,
) -> tuple[bool | None, list[str]]:
    """Does the return value match the tool's declared output type?

    Sprint 3 tightening: pre-Sprint-3 this returned ``True`` with a
    caveat when ``expected_type`` was absent — the grader then
    counted that as a positive. Now: if no type was declared the
    check returns ``None`` ("not applicable") so the grader's
    weighting layer skips the check entirely instead of awarding
    free points.
    """
    if "result" not in event.payload or event.payload["result"] is None:
        return False, ["no result to type-check"]
    expected = event.payload.get("expected_type")
    if not expected:
        return None, ["no expected_type declared (check not applicable)"]
    result = event.payload["result"]
    canonical = _TYPE_NAMES.get(expected.lower())
    if canonical is None:
        return False, [f"unknown expected_type={expected!r}"]
    if isinstance(result, canonical):
        return True, [f"expected={expected} got={type(result).__name__}"]
    return False, [f"expected={expected} got={type(result).__name__}"]


# Side-effect URI scheme registry — schemes whose presence-check is
# implemented inline. Schemes outside this set fall back to a
# "declared but unverified" branch so we never silently award credit
# for unobservable claims.
_VERIFIABLE_SIDE_EFFECT_SCHEMES: frozenset[str] = frozenset({
    "memory",   # memory://<provider>/<key>: provider write — see has_memory_write
    "bus",      # bus://<event_type>: a bus emission produced by the tool
})


def _has_memory_write(payload: dict[str, Any], uri: str) -> bool:
    """Did this tool result include a memory-provider write?

    Sprint 3 extension. The check is intentionally permissive: any of
      * ``memory_writes`` list with at least one entry,
      * ``memory_op`` payload field with ``op == "put"``,
      * URI matches a key listed in ``memory_writes``.
    counts as a positive.
    """
    writes = payload.get("memory_writes") or []
    if writes:
        return True
    mem_op = payload.get("memory_op") or {}
    if isinstance(mem_op, dict) and mem_op.get("op") == "put":
        return True
    # When the URI carries a specific key, look it up explicitly.
    key = uri.split("://", 1)[1] if "://" in uri else uri
    if isinstance(writes, list) and key in writes:
        return True
    return False


def _has_bus_emission(payload: dict[str, Any], uri: str) -> bool:
    """Did this tool record a bus emission of the named event type?"""
    emissions = payload.get("bus_emissions") or []
    if not isinstance(emissions, (list, tuple)):
        return False
    target = uri.split("://", 1)[1] if "://" in uri else uri
    return any(str(e) == target for e in emissions)


def check_side_effect_observable(
    event: BehavioralEvent,
) -> tuple[bool | None, list[str]]:
    """For tools that should mutate state: is the mutation verifiable?

    Returns:
      ``None`` if the tool declared no expected side effects (not
      applicable). The grader skips this check entirely.
      ``True`` if every declared side-effect path/URI exists / fired.
      ``False`` if any declared side effect is missing.

    Sprint 3 extension: in addition to filesystem paths, the check
    now verifies:
      * ``memory://`` URIs against ``payload.memory_writes`` /
        ``payload.memory_op``
      * ``bus://`` URIs against ``payload.bus_emissions``
    Other URI schemes (http://, redis://, …) remain "declared but
    unverified" — they get tagged in evidence but don't count
    positively. Pre-Sprint-3 they silently scored True; that loophole
    is closed.
    """
    declared: list[str] = event.payload.get("expected_side_effects") or []
    if not declared:
        return None, ["no side effects declared"]

    missing: list[str] = []
    present: list[str] = []
    unverified: list[str] = []
    for s in declared:
        if "://" in s:
            scheme = s.split("://", 1)[0].lower()
            if scheme not in _VERIFIABLE_SIDE_EFFECT_SCHEMES:
                # Non-fs, no verifier yet — flag explicitly.
                unverified.append(f"unverified scheme={scheme}: {s}")
                continue
            if scheme == "memory":
                if _has_memory_write(event.payload, s):
                    present.append(f"memory write observed: {s}")
                else:
                    missing.append(f"memory write missing: {s}")
            elif scheme == "bus":
                if _has_bus_emission(event.payload, s):
                    present.append(f"bus emission observed: {s}")
                else:
                    missing.append(f"bus emission missing: {s}")
            continue
        if os.path.exists(s):
            present.append(f"exists: {s}")
        else:
            missing.append(f"missing: {s}")

    if missing:
        return False, missing + present + unverified
    if not present and unverified:
        # Every declared side effect is "unverified scheme". We
        # cannot honestly call this True — return False with the
        # unverified list visible so reviewers can wire the scheme.
        return False, unverified
    return True, present + unverified
