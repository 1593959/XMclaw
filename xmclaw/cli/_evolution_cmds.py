"""``xmclaw evolution`` / ``xmclaw evolve`` subcommands (B-325 split).

Surfaces the SkillRegistry promote/rollback log + Epic #24 review
flow. Lifted out of ``xmclaw/cli/main.py`` so each typer sub-app
gets its own walkable file. ``main.py`` registers this app under
two names (``evolution`` + ``evolve``) for the conversational
shorthand.
"""
from __future__ import annotations

import typer


# ``xmclaw evolution <subcommand>`` — Epic #4 Phase A. Surfaces the
# append-only promote/rollback log that ``SkillRegistry`` persists to
# ``~/.xmclaw/skills/*.jsonl``. Read-only today; Phase B will add the
# live SKILL_PROMOTED/SKILL_ROLLED_BACK event tap.
evolution_app = typer.Typer(
    help=(
        "Inspect / approve / reject SkillRegistry evolution decisions. "
        "Subcommands: show, review, approve, reject (Epic #24)."
    ),
)


@evolution_app.command("show")
def evolution_show(
    since: str | None = typer.Option(
        None, "--since",
        help=(
            "Filter to recent events only. Accepts '24h', '7d', or a bare "
            "integer interpreted as hours. Default: all history."
        ),
    ),
) -> None:
    """Print the skill evolution log as a formatted table.

    Reads every ``<skill_id>.jsonl`` under :func:`xmclaw.utils.paths.skills_dir`
    and prints promotions / rollbacks chronologically. Empty workspace
    prints a friendly notice rather than erroring — a freshly installed
    daemon that hasn't promoted anything yet is a valid state.
    """
    from xmclaw.cli.evolution import run_evolution_show
    raise typer.Exit(code=run_evolution_show(since))


@evolution_app.command("review")
def evolution_review(
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit pending candidates as JSON (scripting-friendly).",
    ),
) -> None:
    """List pending SKILL_CANDIDATE_PROPOSED entries awaiting human review.

    Epic #24 Phase 1: with ``auto_apply=False`` (default), the daemon's
    EvolutionAgent observer publishes candidates onto the bus but never
    moves HEAD — humans approve via ``xmclaw evolve approve <id>``.
    """
    from xmclaw.cli.evolution import run_evolve_review
    raise typer.Exit(code=run_evolve_review(as_json=as_json))


@evolution_app.command("approve")
def evolution_approve(
    candidate_id: str = typer.Argument(
        ..., help="Skill / candidate id from `xmclaw evolve review`.",
    ),
) -> None:
    """Approve a pending candidate (routes through evidence-gated promote).

    Forwards the proposal's evidence list to the daemon's
    ``/api/v2/skills/<id>/promote`` endpoint. Anti-req #12 still enforced
    at the registry door — the registry refuses without evidence.
    """
    from xmclaw.cli.evolution import run_evolve_approve
    raise typer.Exit(code=run_evolve_approve(candidate_id))


@evolution_app.command("reject")
def evolution_reject(
    candidate_id: str = typer.Argument(
        ..., help="Skill / candidate id from `xmclaw evolve review`.",
    ),
    reason: str = typer.Option(
        ..., "--reason", "-r",
        help="Why this candidate is rejected (required, becomes audit row).",
    ),
) -> None:
    """Record a rejection. Does NOT mutate SkillRegistry — HEAD stays put.

    Writes to ``~/.xmclaw/v2/evolution/evo-main/rejections.jsonl`` so the
    audit chain reflects both kinds of decisions.
    """
    from xmclaw.cli.evolution import run_evolve_reject
    raise typer.Exit(code=run_evolve_reject(candidate_id, reason))


@evolution_app.command("migrate-auto-evo")
def evolution_migrate_auto_evo(
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="List what would be migrated without writing anything.",
    ),
) -> None:
    """B-171: salvage skills the deleted xm-auto-evo Node subsystem
    left orphaned in ``~/.xmclaw/auto_evo/skills/``.

    Picks the highest version per lineage, rewrites frontmatter
    (drops auto_created/level/created_at, renames signals_match →
    triggers, injects created_by=evolved + migrated_from), and
    copies to ``~/.xmclaw/skills_user/<auto-kebab-id>/SKILL.md``
    so the boot-time user_loader registers them like any other
    user-installed skill. Existing targets in skills_user are
    NEVER clobbered.

    Use ``--dry-run`` first to see the plan before committing.
    """
    from xmclaw.cli.migrate_auto_evo import migrate
    from xmclaw.utils.paths import data_dir, user_skills_dir

    auto_evo_root = data_dir() / "auto_evo" / "skills"
    target_root = user_skills_dir()

    typer.echo(f"  source: {auto_evo_root}")
    typer.echo(f"  target: {target_root}")
    if dry_run:
        typer.echo("  (dry-run — no files will be written)")
    typer.echo("")

    results = migrate(auto_evo_root, target_root, dry_run=dry_run)
    if not results:
        typer.echo("Nothing to migrate (auto_evo dir empty or missing).")
        raise typer.Exit(code=0)

    for r in results:
        if r.skipped:
            tag = "[skip]"
        elif r.ok:
            tag = "[ok]  "
        else:
            tag = "[FAIL]"
        src = r.source_dir.name if r.source_dir else "?"
        typer.echo(f"  {tag} {r.target_id:<32} ← {src:<40} {r.reason}")

    n_migrated = sum(1 for r in results if r.ok and not r.skipped)
    n_skipped = sum(1 for r in results if r.skipped)
    n_failed = sum(1 for r in results if not r.ok)
    typer.echo("")
    typer.echo(
        f"Done: {n_migrated} migrated, "
        f"{n_skipped} skipped (target exists), "
        f"{n_failed} failed."
    )
    if n_migrated and not dry_run:
        typer.echo("Restart the daemon (xmclaw restart) for skills to register.")
    raise typer.Exit(code=0 if n_failed == 0 else 1)
