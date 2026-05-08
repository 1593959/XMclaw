"""``xmclaw security`` subcommands (B-325 split).

Offline, daemon-independent safety tooling. Lifted out of
``xmclaw/cli/main.py``.
"""
from __future__ import annotations

from pathlib import Path

import typer

# ``xmclaw security <subcommand>`` — Epic #3. Offline, daemon-independent
# safety tooling. ``scan`` runs the SkillScanner over a skill's Python
# source; future siblings (``rules show``, ``policy check``) land here.
security_app = typer.Typer(
    help="Offline security tooling (skill scan, rule inspection).",
)


@security_app.command("scan")
def security_scan(
    path: Path = typer.Argument(
        ..., help="Skill .py file or directory to recursively scan."
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit findings as JSON instead of a human table.",
    ),
) -> None:
    """Scan a skill's Python source for dangerous patterns.

    Runs the YAML rule catalogue (same packs the RuleBasedToolGuardian
    uses) + an AST pass for dynamic-exec, subprocess-shell, pickle /
    marshal deserialization, and ctypes / pty / telnetlib imports.

    Exit code: 0 clean (or LOW / INFO only), 1 on any HIGH / CRITICAL,
    2 on MEDIUM-only findings. Use ``--json`` for CI / tooling
    integration.
    """
    from xmclaw.cli.security_scan import run_security_scan
    raise typer.Exit(code=run_security_scan(path, as_json=as_json))
