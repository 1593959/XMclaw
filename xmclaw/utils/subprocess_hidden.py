"""Hidden-subprocess helpers — Wave 23.

On Windows, ``subprocess.Popen`` / ``asyncio.create_subprocess_exec``
default to inheriting (or allocating) a console. For an agent-runtime
that spawns ``python.exe`` / ``bash.exe`` / ``cmd.exe`` per tool call,
that's a barrage of pop-up windows the user has to deal with.

This module surfaces the right ``creationflags`` / ``startupinfo`` to
keep child processes silent + windowless. On non-Windows it's a noop
(those platforms don't have the problem).

Use in two ways:

  # Sync subprocess.run / Popen
  subprocess.run(cmd, capture_output=True, **hidden_subprocess_kwargs())

  # asyncio subprocess
  await asyncio.create_subprocess_exec(
      *cmd, stdout=PIPE, stderr=PIPE,
      **hidden_subprocess_kwargs(),
  )

The kwargs dict is empty on POSIX so both call sites stay portable.
"""
from __future__ import annotations

import subprocess
import sys
from typing import Any


def hidden_subprocess_kwargs() -> dict[str, Any]:
    """Return platform-appropriate kwargs to suppress a console window.

    On Windows:
      * ``creationflags`` includes ``CREATE_NO_WINDOW`` (0x08000000) —
        no console allocated.
      * ``startupinfo`` further hides the window if one is allocated
        (belt + suspenders for older Python / weird inherit paths).

    Empty dict on macOS / Linux — those platforms don't pop a window.
    """
    if sys.platform != "win32":
        return {}
    # CREATE_NO_WINDOW is the canonical "no console" flag. We do NOT
    # OR with DETACHED_PROCESS — the two are mutually exclusive per
    # Microsoft docs (combining them yields ERROR_INVALID_PARAMETER).
    # Belt-and-suspenders: STARTUPINFO with STARTF_USESHOWWINDOW=SW_HIDE
    # covers the rare case where the child re-creates a window via
    # CreateConsoleScreenBuffer or similar.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE  # type: ignore[attr-defined]
    return {"creationflags": creationflags, "startupinfo": si}


__all__ = ["hidden_subprocess_kwargs"]
