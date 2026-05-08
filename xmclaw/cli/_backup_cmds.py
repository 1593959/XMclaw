"""``xmclaw backup`` subcommands (B-325 split).

Epic #20: create / list / verify / info / delete / prune / restore
of ``~/.xmclaw/`` workspace as portable tar.gz archives. Lifted out
of ``xmclaw/cli/main.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

# ``xmclaw backup <subcommand>`` — Epic #20. ``create`` / ``list`` /
# ``restore`` of the ``~/.xmclaw/`` workspace into portable tar.gz
# archives. Not an alias for ``git`` or ``rsync``: the archive format is
# versioned via ``manifest.json`` so cross-version restores are safe.
backup_app = typer.Typer(
    help="Create, list, and restore backups of the ~/.xmclaw/ workspace.",
)


def _default_backup_source() -> Path:
    from xmclaw.utils.paths import data_dir

    return data_dir()


@backup_app.command("create")
def backup_create(
    name: str = typer.Argument(
        None,
        help="Backup name. Defaults to 'auto-YYYY-MM-DD-HHMMSS'.",
    ),
    source: Path = typer.Option(
        None, "--source",
        help="Workspace to back up. Defaults to $XMC_DATA_DIR or ~/.xmclaw.",
    ),
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to <source>/backups.",
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite",
        help="Replace an existing backup with the same name.",
    ),
) -> None:
    """Archive ``~/.xmclaw/`` to a versioned tar.gz + manifest."""
    import time as _time

    from xmclaw.backup import create_backup
    from xmclaw.backup.create import BackupError

    src = source or _default_backup_source()
    if name is None:
        name = "auto-" + _time.strftime("%Y-%m-%d-%H%M%S", _time.gmtime())
    try:
        manifest = create_backup(
            src, name, backups_dir=dest, overwrite=overwrite,
        )
    except BackupError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"  [ok]  {name}: {manifest.entries} file(s), "
               f"{manifest.archive_bytes} bytes, "
               f"sha256={manifest.archive_sha256[:12]}...")


def _manifest_to_dict(entry: Any) -> dict[str, Any]:
    """Flatten a BackupEntry into a JSON-safe dict for `--json` output.

    Includes the on-disk ``path`` next to the manifest fields so
    scripts can pipe straight into restore / delete without re-
    resolving the backups root.
    """
    m = entry.manifest
    return {
        "name": entry.name,
        "path": str(entry.dir),
        "schema_version": m.schema_version,
        "created_ts": m.created_ts,
        "xmclaw_version": m.xmclaw_version,
        "archive_sha256": m.archive_sha256,
        "archive_bytes": m.archive_bytes,
        "source_dir": m.source_dir,
        "excluded": list(m.excluded),
        "entries": m.entries,
    }


@backup_app.command("list")
def backup_list(
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to ~/.xmclaw/backups.",
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit a JSON array for scripting (stable schema).",
    ),
) -> None:
    """Show every backup on disk.

    Default text mode is columnar for eyeballing; ``--json`` emits a
    stable array with one dict per backup (name / path / all manifest
    fields) — pipe into ``jq`` to filter / sort / feed into another
    ``xmclaw backup ...`` invocation.
    """
    import json as _json

    from xmclaw.backup import list_backups

    entries = list_backups(dest)
    if as_json:
        payload = [_manifest_to_dict(e) for e in entries]
        typer.echo(_json.dumps(payload, indent=2))
        return
    if not entries:
        typer.echo("no backups found.")
        return
    for entry in entries:
        m = entry.manifest
        typer.echo(
            f"  {entry.name:30s}  "
            f"{m.entries:6d} files  "
            f"{m.archive_bytes:>10d} bytes  "
            f"v{m.xmclaw_version}"
        )


@backup_app.command("verify")
def backup_verify(
    name: str = typer.Argument(..., help="Name of the backup to verify."),
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to ~/.xmclaw/backups.",
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help=(
            "Emit a JSON object (`{ok, name, entries, archive_bytes, "
            "archive_sha256}` on pass, `{ok: false, name, error}` on fail). "
            "Exit code still mirrors success — JSON mode is for scripts that "
            "want structured detail, not for suppressing failures."
        ),
    ),
) -> None:
    """Re-hash an existing backup and confirm it still matches its manifest.

    Read-only — does not extract. Use before restoring, after moving a
    backup to slower storage, or to catch bit-rot on long-lived archives.
    Exits non-zero on any failure (missing, corrupt, schema too new,
    checksum drift).

    ``--json`` lets CI / monitoring probes consume a stable dict shape
    instead of parsing the human-readable text; exit code still tracks
    success so `xmclaw backup verify … --json || page-oncall` keeps
    working.
    """
    import json as _json

    from xmclaw.backup import verify_backup
    from xmclaw.backup.restore import RestoreError

    try:
        manifest = verify_backup(name, backups_dir=dest)
    except RestoreError as exc:
        if as_json:
            # Emit the failure dict on stdout (not stderr) so the caller
            # can `xmclaw ... --json | jq .error` uniformly — errors live
            # on the same channel as the success payload, exit code is
            # the tri-state carrier.
            typer.echo(_json.dumps({"ok": False, "name": name, "error": str(exc)}))
        else:
            typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if as_json:
        typer.echo(
            _json.dumps(
                {
                    "ok": True,
                    "name": name,
                    "entries": manifest.entries,
                    "archive_bytes": manifest.archive_bytes,
                    "archive_sha256": manifest.archive_sha256,
                }
            )
        )
        return
    typer.echo(
        f"  [ok]  {name}: sha256 verified "
        f"({manifest.entries} files, {manifest.archive_bytes} bytes)"
    )


def _format_bytes(n: int) -> str:
    """Render ``n`` bytes as KiB/MiB/GiB with one decimal (operator-friendly).

    ``list`` shows raw bytes because the column needs to sort numerically.
    ``info`` is a read-by-one inspector so we can afford to be readable.
    """
    step = 1024.0
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < step:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= step
    return f"{n:.1f} PiB"


@backup_app.command("info")
def backup_info(
    name: str = typer.Argument(..., help="Name of the backup to inspect."),
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to ~/.xmclaw/backups.",
    ),
    show_excluded: bool = typer.Option(
        False, "--show-excluded",
        help="Also print the list of glob patterns that were excluded.",
    ),
    as_json: bool = typer.Option(
        False, "--json",
        help="Emit the full manifest as a JSON dict (implies --show-excluded).",
    ),
) -> None:
    """Pretty-print a single backup's manifest without re-hashing.

    Cheaper than ``verify`` — this only reads ``manifest.json`` and
    echoes the metadata. Use when you want to know *what* a backup is
    (when it was taken, what version, how big) without paying the
    sha256 cost. Exits non-zero when the backup is missing or malformed.

    ``--json`` emits the same dict shape as ``backup list --json``
    produces for each element (always includes the full ``excluded``
    list).
    """
    import datetime as _dt
    import json as _json

    from xmclaw.backup import get_backup
    from xmclaw.backup.store import BackupNotFoundError

    try:
        entry = get_backup(name, backups_dir=dest)
    except (BackupNotFoundError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if as_json:
        typer.echo(_json.dumps(_manifest_to_dict(entry), indent=2))
        return

    m = entry.manifest
    created = _dt.datetime.fromtimestamp(
        m.created_ts, tz=_dt.timezone.utc
    ).isoformat(timespec="seconds")
    typer.echo(f"  name           {entry.name}")
    typer.echo(f"  path           {entry.dir}")
    typer.echo(f"  created        {created}")
    typer.echo(f"  xmclaw_version {m.xmclaw_version}")
    typer.echo(f"  source_dir     {m.source_dir}")
    typer.echo(f"  entries        {m.entries}")
    typer.echo(f"  archive_bytes  {m.archive_bytes} ({_format_bytes(m.archive_bytes)})")
    typer.echo(f"  sha256         {m.archive_sha256[:16]}…")
    typer.echo(f"  schema_version {m.schema_version}")
    if show_excluded:
        if m.excluded:
            typer.echo("  excluded:")
            for pat in m.excluded:
                typer.echo(f"    - {pat}")
        else:
            typer.echo("  excluded       (none)")


@backup_app.command("delete")
def backup_delete(
    name: str = typer.Argument(..., help="Name of the backup to delete."),
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to ~/.xmclaw/backups.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Remove a single backup directory (archive + manifest)."""
    from xmclaw.backup import delete_backup
    from xmclaw.backup.store import BackupNotFoundError

    if not yes:
        confirm = typer.confirm(
            f"delete backup {name!r}? this cannot be undone",
            default=False,
        )
        if not confirm:
            typer.echo("aborted.")
            raise typer.Exit(code=1)
    try:
        path = delete_backup(name, backups_dir=dest)
    except BackupNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"  [ok]  deleted {path}")


@backup_app.command("prune")
def backup_prune(
    keep: int = typer.Option(
        5, "--keep",
        help="Number of newest backups to retain. Older ones are deleted.",
    ),
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to ~/.xmclaw/backups.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Keep only the ``--keep`` newest backups; drop the rest."""
    from xmclaw.backup import list_backups, prune_backups

    entries = list_backups(dest)
    if len(entries) <= keep:
        typer.echo(
            f"nothing to prune: {len(entries)} backup(s) <= keep={keep}."
        )
        return
    will_remove = entries[: len(entries) - keep]
    if not yes:
        typer.echo(f"would remove {len(will_remove)} backup(s):")
        for e in will_remove:
            typer.echo(f"  - {e.name}")
        if not typer.confirm("proceed?", default=False):
            typer.echo("aborted.")
            raise typer.Exit(code=1)
    try:
        removed = prune_backups(backups_dir=dest, keep=keep)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"  [ok]  removed {len(removed)} backup(s).")


@backup_app.command("restore")
def backup_restore(
    name: str = typer.Argument(..., help="Name of the backup to restore."),
    target: Path = typer.Option(
        None, "--target",
        help="Destination workspace. Defaults to $XMC_DATA_DIR or ~/.xmclaw.",
    ),
    dest: Path = typer.Option(
        None, "--dest",
        help="Backups directory. Defaults to ~/.xmclaw/backups.",
    ),
    keep_previous: bool = typer.Option(
        True, "--keep-previous/--no-keep-previous",
        help=(
            "When the target exists, move it aside to <target>.prev-<ts> "
            "before extracting (default on — lets you roll back a bad "
            "restore)."
        ),
    ),
) -> None:
    """Extract a backup back into the workspace.

    Does not stop or restart the daemon. Run ``xmclaw stop`` first; after
    the restore completes, run ``xmclaw start`` to bring it back up.
    """
    from xmclaw.backup import restore_backup
    from xmclaw.backup.restore import RestoreError

    tgt = target or _default_backup_source()
    try:
        result = restore_backup(
            name, tgt, backups_dir=dest, keep_previous=keep_previous,
        )
    except RestoreError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"  [ok]  restored {name} -> {result}")
    typer.echo("    next: run 'xmclaw start' to bring the daemon back up.")
