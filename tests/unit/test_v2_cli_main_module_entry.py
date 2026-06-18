"""B-343 regression — ``python -m xmclaw.cli.main`` must run the Typer app.

``xmclaw.daemon.lifecycle.start_daemon`` spawns the daemon detached
via::

    [sys.executable, "-m", "xmclaw.cli.main", "serve", ...]

That command relies on ``xmclaw/cli/main.py`` having an
``if __name__ == "__main__": app()`` block — without it Python imports
the module and exits silently, so ``xmclaw start`` reports "daemon
exited before becoming healthy" with an empty daemon.log.

The B-325 monolith split (commit ``cf4d624``) accidentally truncated
the trailing block when extracting ``_config_cmds`` to its own file.
The console-script entry point still worked (pyproject's
``xmclaw = xmclaw.cli.main:app`` calls ``app()`` explicitly) so
``CliRunner.invoke(app, ...)`` tests stayed green — but ``xmclaw
start`` was silently broken until B-343 restored the block.

These tests run the CLI as a subprocess so the import-side bug is
visible; CliRunner won't catch it because it bypasses the entry
shape entirely.
"""
from __future__ import annotations

import subprocess
import sys


def _run_cli(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    """Invoke ``python -m xmclaw.cli.main`` with the given args and
    return the completed process. Captures stdout + stderr."""
    return subprocess.run(
        [sys.executable, "-u", "-m", "xmclaw.cli.main", *args],
        capture_output=True,
        text=True,
        # The CLI forces UTF-8 stdout (main.py) so help glyphs like ↔ render
        # on CN-locale consoles; decode with the same codec here, otherwise
        # subprocess.run's default (gbk on Windows) can't decode the output.
        encoding="utf-8",
        timeout=timeout,
    )


def test_b343_module_entry_runs_app_on_help() -> None:
    """``python -m xmclaw.cli.main --help`` must print the Typer help
    page and exit 0. Pre-B-343 it imported the module and exited 0
    with EMPTY stdout — the missing ``if __name__ == "__main__"``
    block meant ``app()`` never fired.
    """
    cp = _run_cli("--help")
    assert cp.returncode == 0, (
        f"CLI --help exited {cp.returncode}; stderr={cp.stderr!r}"
    )
    # Help output must be non-empty and look like Typer's usage line.
    assert cp.stdout, "CLI --help produced no stdout — silent-import regression"
    assert "Usage:" in cp.stdout or "Commands" in cp.stdout, (
        f"CLI --help stdout doesn't look like Typer help: {cp.stdout[:300]!r}"
    )


def test_b343_module_entry_runs_app_on_version() -> None:
    """``python -m xmclaw.cli.main version`` must print the version
    string. Same regression class — if the entry shape is broken,
    every subcommand silently no-ops."""
    cp = _run_cli("version")
    assert cp.returncode == 0, (
        f"CLI version exited {cp.returncode}; stderr={cp.stderr!r}"
    )
    assert cp.stdout.startswith("xmclaw v"), (
        f"CLI version stdout={cp.stdout!r}; expected 'xmclaw v...'"
    )


def test_b343_lifecycle_command_uses_module_entry_form() -> None:
    """``xmclaw.daemon.lifecycle.start_daemon`` builds the spawn
    command as ``[python, -m, xmclaw.cli.main, serve, ...]`` —
    confirms the module path lifecycle relies on so a future
    refactor that switches to ``xmclaw.cli`` (the package's
    ``__main__.py`` form) wouldn't silently invalidate this
    regression test."""
    import inspect

    from xmclaw.daemon import lifecycle

    src = inspect.getsource(lifecycle.start_daemon)
    # The exact module path lifecycle relies on.
    assert '"-m", "xmclaw.cli.main"' in src, (
        "lifecycle.start_daemon no longer spawns via ``-m "
        "xmclaw.cli.main``; if you changed the spawn form, also "
        "update this test (and confirm the new form has its own "
        "module-entry guard)."
    )
