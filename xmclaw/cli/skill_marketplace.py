"""B-390: ``xmclaw skill *`` Typer subcommands — skill marketplace MVP.

Sibling of the existing ``_<area>_cmds.py`` pattern (``_config_cmds`` etc).
Wired into the root app from :mod:`xmclaw.cli.main`.

Subcommands:

* ``xmclaw skill list-marketplace``  — print the curated catalog
* ``xmclaw skill search <query>``    — substring/tag match against the
  catalog
* ``xmclaw skill install <id>``      — clone + scan + register
* ``xmclaw skill remove <id>``       — uninstall a previously-installed
  skill
* ``xmclaw skill installed``         — list marketplace-installed
  skills

Implementation note: this module is the user-facing edge. All HTTP
fetching / git cloning / scanner integration lives in
:mod:`xmclaw.skills.marketplace` so the daemon router shares the same
flow.

Why ``xmclaw skill`` (singular) and not ``xmclaw skills``: matches
``xmclaw config`` / ``xmclaw memory`` / ``xmclaw security`` — Typer
groups already use the singular and double-pluralisation reads weirdly
(``xmclaw skills install`` vs ``xmclaw skill install``).
"""
from __future__ import annotations

import json as _json
from typing import Any

import typer

from xmclaw.skills.marketplace import (
    InstallScanFailed,
    InstallValidationError,
    MarketplaceError,
    SkillNotInIndexError,
    fetch_index,
    install,
    list_installed,
    remove,
)

skill_app = typer.Typer(
    help=(
        "Browse + install skills from the curated XMclaw marketplace. "
        "B-390 (Sprint 2). Skills install into ~/.xmclaw/skills_user/<id>/, "
        "the same canonical root the daemon's UserSkillsLoader scans on "
        "boot — restart the daemon (or wait for the watcher tick) to pick "
        "up new installs."
    ),
)


def _print_skill_row(s: Any, *, show_tags: bool = True) -> None:
    """One-line catalog entry. Output stays narrow (~80 cols) so wrapped
    terminals don't break alignment."""
    badge = "✓" if s.trust_tier == "verified" else "·"
    line = f"  {badge} {s.id:<28} v{s.version:<8} {s.name}"
    typer.echo(line)
    if s.description:
        typer.echo(f"      {s.description}")
    if show_tags and s.tags:
        typer.echo(f"      tags: {', '.join(s.tags)} | author: {s.author or '?'}")


@skill_app.command("list-marketplace")
def list_marketplace(
    refresh: bool = typer.Option(
        False, "--refresh",
        help="Bypass the 1-hour cache and re-fetch the catalog.",
    ),
    json_output: bool = typer.Option(
        False, "--json",
        help="Emit the raw index JSON for scripting / piping.",
    ),
) -> None:
    """Print the curated XMclaw skill catalog.

    Reads ``https://raw.githubusercontent.com/.../skill_marketplace_index.json``
    (overridable via ``XMC_SKILL_MARKETPLACE_URL``) with a 1-hour cache.
    """
    try:
        idx = fetch_index(refresh=refresh)
    except MarketplaceError as exc:
        typer.echo(f"  [x]  marketplace error: {exc}", err=True)
        raise typer.Exit(code=1)

    if json_output:
        typer.echo(_json.dumps(idx.to_dict(), ensure_ascii=False, indent=2))
        return

    typer.echo(f"xmclaw skill marketplace ({idx.updated}, {len(idx.skills)} skills)")
    typer.echo("")
    for s in idx.skills:
        _print_skill_row(s)
        typer.echo("")
    typer.echo("  install: xmclaw skill install <id>")


@skill_app.command("search")
def search(
    query: str = typer.Argument(..., help="Substring / tag to match against name, description, tags, author."),
    refresh: bool = typer.Option(False, "--refresh"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Filter the marketplace index by substring or tag."""
    try:
        idx = fetch_index(refresh=refresh)
    except MarketplaceError as exc:
        typer.echo(f"  [x]  marketplace error: {exc}", err=True)
        raise typer.Exit(code=1)
    matches = idx.search(query)
    if json_output:
        typer.echo(_json.dumps(
            {"query": query, "matches": [s.to_dict() for s in matches]},
            ensure_ascii=False, indent=2,
        ))
        return
    typer.echo(f"xmclaw skill search {query!r} — {len(matches)} match(es)")
    typer.echo("")
    if not matches:
        typer.echo("  (no matches)")
        return
    for s in matches:
        _print_skill_row(s)
        typer.echo("")


@skill_app.command("install")
def install_cmd(
    skill_id: str = typer.Argument(..., help="Skill id from the marketplace catalog."),
    refresh: bool = typer.Option(False, "--refresh", help="Re-fetch the catalog before resolving the id."),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the install-confirmation prompt (e.g. for CI / scripts).",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Clone a skill from the catalog into ``~/.xmclaw/skills_user/<id>/``.

    Runs the security scanner against every ``*.py`` in the cloned tree
    before recording the install — any CRITICAL finding aborts and the
    half-cloned directory is rolled back.
    """
    try:
        idx = fetch_index(refresh=refresh)
    except MarketplaceError as exc:
        typer.echo(f"  [x]  marketplace error: {exc}", err=True)
        raise typer.Exit(code=1)

    skill = idx.find(skill_id)
    if skill is None:
        typer.echo(
            f"  [x]  skill {skill_id!r} not in marketplace; "
            f"run `xmclaw skill list-marketplace` to see available ids",
            err=True,
        )
        raise typer.Exit(code=1)

    if not yes:
        typer.echo(f"about to install {skill.id} v{skill.version} from {skill.source}")
        typer.echo(f"  trust tier: {skill.trust_tier}  license: {skill.license or '?'}")
        if not typer.confirm("  proceed?", default=True):
            typer.echo("  [!]   aborted")
            raise typer.Exit(code=2)

    try:
        result = install(skill_id, index=idx)
    except SkillNotInIndexError as exc:  # pragma: no cover — guarded above
        typer.echo(f"  [x]  {exc}", err=True)
        raise typer.Exit(code=1)
    except InstallScanFailed as exc:
        typer.echo(f"  [x]  install rejected: {exc}", err=True)
        for f in exc.findings[:5]:
            typer.echo(
                f"        {f['severity']:<8} {f['rule_id']} ({f['file']}): {f['title']}",
                err=True,
            )
        raise typer.Exit(code=1)
    except InstallValidationError as exc:
        typer.echo(f"  [x]  validation failed: {exc}", err=True)
        raise typer.Exit(code=1)
    except MarketplaceError as exc:
        typer.echo(f"  [x]  install error: {exc}", err=True)
        raise typer.Exit(code=1)

    if json_output:
        typer.echo(_json.dumps({
            "ok": True,
            "skill_id": result.skill_id,
            "version": result.version,
            "install_path": str(result.install_path),
            "source": result.source,
            "findings": result.findings,
        }, ensure_ascii=False, indent=2))
        return

    typer.echo(f"  [ok]  installed {result.skill_id} v{result.version}")
    typer.echo(f"        path: {result.install_path}")
    if result.findings:
        typer.echo(
            f"        scanner: {len(result.findings)} non-critical finding(s) "
            "— run `xmclaw security scan <path>` for details"
        )
    typer.echo(
        "        next: restart the daemon (`xmclaw restart`) "
        "or wait for the watcher tick to pick up the new skill"
    )


@skill_app.command("remove")
def remove_cmd(
    skill_id: str = typer.Argument(..., help="Skill id to uninstall."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Remove a previously-installed marketplace skill.

    Deletes both ``~/.xmclaw/skills_user/<id>/`` and the corresponding
    entry in the install registry. Idempotent — removing a skill that
    isn't installed prints a warning and exits non-zero so scripts can
    detect "nothing to do" cases.
    """
    if not yes:
        if not typer.confirm(
            f"  remove {skill_id} from ~/.xmclaw/skills_user/?", default=False,
        ):
            typer.echo("  [!]   aborted")
            raise typer.Exit(code=2)
    removed = remove(skill_id)
    if not removed:
        typer.echo(f"  [!]   {skill_id} was not installed — nothing to remove")
        raise typer.Exit(code=1)
    typer.echo(f"  [ok]  removed {skill_id}")
    typer.echo(
        "        next: restart the daemon (`xmclaw restart`) "
        "to drop the skill from the registry"
    )


@skill_app.command("installed")
def installed(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List marketplace-installed skills.

    This shows entries from ``~/.xmclaw/skills_user/.marketplace.json`` —
    the install registry the marketplace CLI maintains. Hand-installed
    skills (``git clone``'d directly into the user-skills dir) won't
    appear here; use the daemon's ``/api/v2/skills`` API or the Skills
    page in the UI for the canonical "what skills does my agent have?"
    listing.
    """
    rows = list_installed()
    if json_output:
        typer.echo(_json.dumps(
            {"skills": [r.to_dict() for r in rows]},
            ensure_ascii=False, indent=2,
        ))
        return
    typer.echo(f"xmclaw skill installed ({len(rows)})")
    typer.echo("")
    if not rows:
        typer.echo(
            "  (none)\n"
            "  install via: xmclaw skill install <id>\n"
            "  browse:      xmclaw skill list-marketplace"
        )
        return
    for r in rows:
        typer.echo(f"  {r.id:<28} v{r.version:<8} {r.name}")
        typer.echo(f"      source: {r.source}")
        typer.echo(f"      path:   {r.install_path}")
        typer.echo(f"      tier:   {r.trust_tier}")
        typer.echo("")
