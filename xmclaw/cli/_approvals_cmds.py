"""``xmclaw approvals`` subcommands (B-325 split).

Manages pending security approvals created by the GuardedToolProvider
when a tool call hits the HIGH-severity needs-approval branch.
Lifted out of ``xmclaw/cli/main.py``.
"""
from __future__ import annotations

import typer

# ``xmclaw approvals <subcommand>`` — Epic #3. List, approve, or deny
# pending security approvals created by the GuardedToolProvider.
approvals_app = typer.Typer(
    help="Manage pending security approvals for guarded tool calls.",
)


@approvals_app.command("list")
def approvals_list() -> None:
    """List all pending security approvals."""
    from xmclaw.cli.approvals import run_approvals_list
    raise typer.Exit(code=run_approvals_list())


@approvals_app.command("approve")
def approvals_approve(
    request_id: str = typer.Argument(..., help="The approval request ID."),
) -> None:
    """Approve a pending security request."""
    from xmclaw.cli.approvals import run_approvals_approve
    raise typer.Exit(code=run_approvals_approve(request_id))


@approvals_app.command("deny")
def approvals_deny(
    request_id: str = typer.Argument(..., help="The approval request ID."),
) -> None:
    """Deny a pending security request."""
    from xmclaw.cli.approvals import run_approvals_deny
    raise typer.Exit(code=run_approvals_deny(request_id))
