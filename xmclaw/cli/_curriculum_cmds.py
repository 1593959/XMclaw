"""``xmclaw curriculum`` subcommands (B-325 split).

B-200 / Phase 5. Review and action the agent's self-proposed edits to
LEARNING.md. Lifted out of ``xmclaw/cli/main.py``.
"""
from __future__ import annotations

import typer

# ``xmclaw curriculum <subcommand>`` — B-200 / Phase 5. Review and
# action the agent's self-proposed edits to LEARNING.md (its learning
# rules). The agent files via the ``propose_curriculum_edit`` tool;
# the user approves / rejects here. Hard human-in-loop is the
# Goodhart defence — agent can't silently rewrite its own rule book.
curriculum_app = typer.Typer(
    help=(
        "Review agent-proposed edits to LEARNING.md (the agent's "
        "self-modifying learning rules). Subcommands: list, show, "
        "approve, reject."
    ),
)


@curriculum_app.command("list")
def curriculum_list(
    status: str = typer.Option(
        "pending", "--status",
        help="Filter: pending | approved | rejected | all (default: pending)",
    ),
) -> None:
    """List the agent's curriculum-edit proposals.

    Each proposal asks to add a principle to LEARNING.md (the agent's
    learning rules). Pending proposals haven't been acted on; the
    agent's system prompt won't reflect them until you ``approve``.
    """
    from xmclaw.cli.curriculum import run_curriculum_list
    raise typer.Exit(code=run_curriculum_list(status))


@curriculum_app.command("show")
def curriculum_show(
    proposal_id: str = typer.Argument(..., help="The proposal ID."),
) -> None:
    """Show the full text + rationale + evidence of one proposal."""
    from xmclaw.cli.curriculum import run_curriculum_show
    raise typer.Exit(code=run_curriculum_show(proposal_id))


@curriculum_app.command("approve")
def curriculum_approve(
    proposal_id: str = typer.Argument(..., help="The proposal ID."),
) -> None:
    """Approve a curriculum-edit proposal — applies it to LEARNING.md
    and refreshes the on-disk render so the next agent turn picks
    up the new rule.
    """
    from xmclaw.cli.curriculum import run_curriculum_approve
    raise typer.Exit(code=run_curriculum_approve(proposal_id))


@curriculum_app.command("reject")
def curriculum_reject(
    proposal_id: str = typer.Argument(..., help="The proposal ID."),
    reason: str = typer.Option(
        "", "--reason", "-r",
        help="Optional reason. Visible to the agent on next "
             "list_curriculum_proposals call so it can learn from the "
             "rejection.",
    ),
) -> None:
    """Reject a curriculum-edit proposal — marks it rejected without
    applying. The reason (if provided) is stored so the agent sees
    why and can avoid filing the same proposal again."""
    from xmclaw.cli.curriculum import run_curriculum_reject
    raise typer.Exit(code=run_curriculum_reject(proposal_id, reason))
