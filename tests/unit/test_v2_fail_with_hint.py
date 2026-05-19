"""Epic #27 sweep #16 (2026-05-19): _fail_with_hint structured error.

Pre-fix many tool exception sites returned raw ``f"{type(exc).__name__}:
{exc}"`` — accurate but not actionable. The LLM couldn't tell from
"PermissionError: ..." whether the fix was "try a different path"
or "ask the user to elevate" or "the file is locked by another
process". The new helper packages a summary + the exception + an
actionable hint into a pipe-separated error string.
"""
from __future__ import annotations

import time

from xmclaw.core.ir import ToolCall
from xmclaw.providers.tool._helpers import _fail, _fail_with_hint


def test_fail_with_hint_includes_all_parts() -> None:
    """summary + exc + hint all show up in the error string."""
    call = ToolCall(name="file_write", args={}, provenance="test")
    t0 = time.perf_counter()
    exc = PermissionError("[Errno 13] Permission denied: '/foo/bar'")
    out = _fail_with_hint(
        call, t0,
        "file_write blocked",
        exc=exc,
        hint="check file is not open in editor",
    )
    assert out.ok is False
    err = out.error or ""
    assert "file_write blocked" in err
    assert "PermissionError" in err
    assert "Permission denied" in err
    assert "hint: check file is not open" in err
    # Pipe-separated for easy parsing downstream.
    assert err.count("|") == 2


def test_fail_with_hint_omits_optional_parts() -> None:
    """No exc + no hint → behaves like _fail (single-part summary)."""
    call = ToolCall(name="x", args={}, provenance="test")
    out = _fail_with_hint(call, time.perf_counter(), "bare summary")
    assert out.ok is False
    assert out.error == "bare summary"


def test_fail_with_hint_exc_only_no_hint() -> None:
    """exc without hint produces "summary | exc-type: exc-msg"."""
    call = ToolCall(name="x", args={}, provenance="test")
    out = _fail_with_hint(
        call, time.perf_counter(),
        "could not open",
        exc=FileNotFoundError("no such file: /tmp/missing"),
    )
    err = out.error or ""
    assert "could not open" in err
    assert "FileNotFoundError" in err
    assert "hint:" not in err


def test_fail_back_compat() -> None:
    """The plain ``_fail`` helper still works — _fail_with_hint is
    additive, not a replacement."""
    call = ToolCall(name="x", args={}, provenance="test")
    out = _fail(call, time.perf_counter(), "bad input")
    assert out.error == "bad input"
