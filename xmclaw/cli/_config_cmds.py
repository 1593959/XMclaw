"""``xmclaw config`` subcommands (B-325 split).

Create or tweak ``daemon/config.json``: init / set / unset / get /
show plus the secret-management commands (set-secret / get-secret /
delete-secret / list-secrets / migrate-secrets). Lifted out of
``xmclaw/cli/main.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

# ``xmclaw config <subcommand>`` — ``init`` writes a fresh daemon/config.json,
# ``set`` mutates a dotted key. README has been advertising both since v2
# rewrite; this typer group is what makes those promises real.
config_app = typer.Typer(
    help="Create or tweak daemon/config.json (LLM keys, tools, gateway).",
)


def _resolve_config_path(path: str) -> Path:
    raw = Path(path)
    if str(raw).replace("\\", "/") == "daemon/config.json":
        from xmclaw.utils.paths import data_dir
        return data_dir() / "config.json"
    return raw


def _default_config_template() -> dict:
    """Thin wrapper around the shared template so this module keeps its
    import surface stable. See :mod:`xmclaw.cli.config_template`."""
    from xmclaw.cli.config_template import default_config_template
    return default_config_template()


def _parse_dotted_value(raw: str):
    """``config set`` argument parsing: JSON literal first, then string.

    ``xmclaw config set gateway.port 9000`` -> int 9000.
    ``xmclaw config set llm.anthropic.api_key sk-ant-xxx`` -> string.
    ``xmclaw config set evolution.enabled true`` -> bool True.
    """
    import json as _json
    try:
        return _json.loads(raw)
    except _json.JSONDecodeError:
        return raw


@config_app.command("init")
def config_init(
    path: str = typer.Option(
        "daemon/config.json", "--path",
        help="Where to write the config (default: daemon/config.json).",
    ),
    provider: str = typer.Option(
        "", "--provider",
        help="Optional: pre-set the default LLM provider ('anthropic' or 'openai').",
    ),
    api_key: str = typer.Option(
        "", "--api-key",
        help="Optional: populate the chosen provider's api_key non-interactively.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Overwrite an existing config file.",
    ),
) -> None:
    """Write a fresh daemon/config.json skeleton.

    The skeleton covers the three sections the daemon needs to boot:
    ``llm`` (with empty api_key placeholders for both providers),
    ``gateway``, and ``security.prompt_injection``. Anything else
    (``tools``, ``memory``, ``evolution``, ``mcp_servers``,
    ``integrations``) defaults at daemon level and can be added by hand
    from ``daemon/config.example.json`` when you need it.

    Refuses to overwrite an existing file unless ``--force`` is passed,
    so re-running this command is always safe.
    """
    import json as _json
    from pathlib import Path as _Path

    target = _resolve_config_path(path)
    if target.exists() and not force:
        typer.echo(
            f"  [!]   config already exists at {target}", err=True,
        )
        typer.echo(
            "        pass --force to overwrite, or use "
            "'xmclaw config set <key> <value>' to edit in place",
            err=True,
        )
        raise typer.Exit(code=1)

    if provider and provider not in ("anthropic", "openai"):
        typer.echo(
            f"  [x]  unknown provider '{provider}' "
            "(expected 'anthropic' or 'openai')",
            err=True,
        )
        raise typer.Exit(code=2)

    template = _default_config_template()
    if provider:
        template["llm"]["default_provider"] = provider
        if api_key:
            template["llm"][provider]["api_key"] = api_key

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _json.dumps(template, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    typer.echo(f"  [ok]  wrote {target}")
    if not api_key:
        typer.echo(
            "        next: set an LLM api_key -- e.g. "
            "'xmclaw config set llm.anthropic.api_key sk-ant-...' "
            "or edit the file directly"
        )
    typer.echo("        then run 'xmclaw doctor' to verify")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(
        ..., help="Dotted key path, e.g. 'llm.anthropic.api_key'.",
    ),
    value: str = typer.Argument(
        ..., help="Value. Parsed as JSON when valid (true/false/123/[...]); "
                  "otherwise treated as a string.",
    ),
    path: str = typer.Option(
        "daemon/config.json", "--path",
        help="Config file to mutate (default: daemon/config.json).",
    ),
) -> None:
    """Set one dotted key in a config JSON file.

    Creates intermediate objects as needed. Refuses to touch a missing
    file (run 'xmclaw config init' first) or one that isn't a JSON
    object at its root -- the daemon factory expects ``dict`` and
    nothing else.
    """
    import json as _json
    from pathlib import Path as _Path

    target = _resolve_config_path(path)
    if not target.exists():
        typer.echo(
            f"  [x]  no config at {target} -- run 'xmclaw config init' first",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        data = _json.loads(target.read_text(encoding="utf-8"))
    except _json.JSONDecodeError as exc:
        typer.echo(f"  [x]  {target} is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1)
    if not isinstance(data, dict):
        typer.echo(
            f"  [x]  {target} must have a JSON object at its root, "
            f"got {type(data).__name__}",
            err=True,
        )
        raise typer.Exit(code=1)

    parts = [p for p in key.split(".") if p]
    if not parts:
        typer.echo("  [x]  key must be non-empty", err=True)
        raise typer.Exit(code=2)

    parsed_value = _parse_dotted_value(value)

    cursor = data
    for segment in parts[:-1]:
        existing = cursor.get(segment)
        if not isinstance(existing, dict):
            # Either missing or a scalar that we need to overwrite with a
            # dict to make room for the nested key. Overwriting a scalar
            # mid-path is deliberate: 'config set llm.anthropic.x 1'
            # against a config where 'llm.anthropic' was accidentally set
            # to "" should recover rather than error-out.
            cursor[segment] = {}
            existing = cursor[segment]
        cursor = existing
    cursor[parts[-1]] = parsed_value

    target.write_text(
        _json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    typer.echo(f"  [ok]  {target}: {key} = {_json.dumps(parsed_value)}")


# ── xmclaw config set-secret / get-secret / delete-secret / list-secrets ──
# Epic #16 Phase 1 entry. Thin shell over xmclaw.utils.secrets. Kept in
# the config_app group so users find all credential-management commands
# together. Writes default to the file backend; callers can opt into
# the OS keyring with --backend keyring (requires `keyring` installed).


@config_app.command("set-secret")
def config_set_secret(
    name: str = typer.Argument(
        ..., help="Secret name, e.g. 'llm.anthropic.api_key'.",
    ),
    value: str = typer.Option(
        None, "--value",
        help=(
            "Plaintext value. Omit to read from stdin (safer — value "
            "does not land in shell history)."
        ),
    ),
    backend: str = typer.Option(
        "file", "--backend",
        help="Where to store: 'file' (~/.xmclaw/secrets.json) or 'keyring'.",
    ),
) -> None:
    """Store a secret in the chosen backend."""
    import sys as _sys

    from xmclaw.utils.secrets import set_secret

    if value is None:
        # Read from stdin without echoing to avoid shell-history leaks.
        # getpass doesn't work reliably when stdin isn't a tty (CI),
        # so fall back to a line-read there.
        if _sys.stdin.isatty():
            import getpass

            value = getpass.getpass(f"value for {name}: ")
        else:
            value = _sys.stdin.readline().rstrip("\n")
    if not value:
        typer.echo("  [x]  empty value refused", err=True)
        raise typer.Exit(code=2)
    try:
        set_secret(name, value, backend=backend)  # type: ignore[arg-type]
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"  [x]  {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"  [ok]  stored {name} in {backend} backend")


@config_app.command("get-secret")
def config_get_secret(
    name: str = typer.Argument(..., help="Secret name to resolve."),
    reveal: bool = typer.Option(
        False, "--reveal",
        help=(
            "Print the plaintext value. Default only prints the source "
            "backend and a preview — safer for screen-share / logs."
        ),
    ),
) -> None:
    """Resolve a secret through env > file > keyring precedence."""
    from xmclaw.utils.secrets import _env_var_for, get_secret

    val = get_secret(name)
    if val is None:
        typer.echo(
            f"  [!]  {name} not set "
            f"(tried env {_env_var_for(name)}, secrets.json, keyring)"
        )
        raise typer.Exit(code=1)
    if reveal:
        typer.echo(val)
        return
    # Non-reveal: show length + first 2/last 2 chars so the user can
    # tell "did I set the right key" without leaking the full secret.
    if len(val) <= 4:
        preview = "*" * len(val)
    else:
        preview = f"{val[:2]}{'*' * (len(val) - 4)}{val[-2:]}"
    typer.echo(f"  [ok]  {name}: {preview}  (len={len(val)})")


@config_app.command("delete-secret")
def config_delete_secret(
    name: str = typer.Argument(..., help="Secret name to delete."),
) -> None:
    """Remove a secret from file and keyring layers (env is read-only)."""
    from xmclaw.utils.secrets import delete_secret

    deleted = delete_secret(name)
    if deleted:
        typer.echo(f"  [ok]  removed {name}")
    else:
        typer.echo(f"  [!]  {name} was not set in any writable backend")


@config_app.command("list-secrets")
def config_list_secrets() -> None:
    """List the names of secrets in the file backend."""
    from xmclaw.utils.secrets import (
        iter_env_override_names,
        list_secret_names,
        secrets_file_path,
    )

    names = list_secret_names()
    if not names:
        typer.echo(f"no secrets at {secrets_file_path()}")
        return
    env_overrides = set(iter_env_override_names())
    for n in names:
        marker = "  (overridden by env)" if n in env_overrides else ""
        typer.echo(f"  {n}{marker}")


# ── xmclaw config migrate-secrets (Epic #16 Phase 2) ──────────────────
# One-shot migration from the legacy plaintext ``~/.xmclaw/secrets.json``
# into the Fernet-encrypted ``~/.xmclaw.secret/secrets.enc`` store.
# Idempotent by design: re-running is safe and reports "nothing to do".
# Conflicts (a name present in both layers with *different* values) are
# surfaced as a failure-to-reconcile rather than silently picking a
# winner — the operator chooses via explicit ``set-secret`` /
# ``delete-secret`` before re-running.


@config_app.command("migrate-secrets")
def config_migrate_secrets(
    wipe: bool = typer.Option(
        True, "--wipe/--no-wipe",
        help=(
            "After a clean migration, remove the plaintext secrets.json "
            "so `grep -r 'sk-' ~/.xmclaw/` stops matching. --no-wipe keeps "
            "the plaintext copy for ops verification."
        ),
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Report what would happen without writing the encrypted store.",
    ),
) -> None:
    """Move secrets.json → Fernet-encrypted store (Epic #16 Phase 2)."""
    from xmclaw.utils.secrets import (
        _load_encrypted,
        _load_file,
        is_encryption_available,
        migrate_plaintext_to_encrypted,
        secrets_file_path,
        encrypted_secrets_path,
    )

    if not is_encryption_available():
        typer.echo(
            "  [x]  cryptography package not installed; cannot encrypt.\n"
            "       Run `pip install cryptography` and retry.",
            err=True,
        )
        raise typer.Exit(code=1)

    if dry_run:
        plaintext = _load_file()
        encrypted = _load_encrypted()
        new = [k for k in plaintext if k not in encrypted]
        conflicts = [
            k for k in plaintext
            if k in encrypted and encrypted[k] != plaintext[k]
        ]
        same = [
            k for k in plaintext
            if k in encrypted and encrypted[k] == plaintext[k]
        ]
        typer.echo(f"  [dry-run]  plaintext:  {secrets_file_path()}")
        typer.echo(f"  [dry-run]  encrypted:  {encrypted_secrets_path()}")
        typer.echo(f"  [dry-run]  would migrate:         {len(new)}")
        typer.echo(f"  [dry-run]  identical (skip):      {len(same)}")
        typer.echo(f"  [dry-run]  conflicts (block):     {len(conflicts)}")
        if conflicts:
            typer.echo("  [dry-run]  conflicting names:")
            for name in conflicts:
                typer.echo(f"    - {name}")
        return

    try:
        result = migrate_plaintext_to_encrypted(wipe_plaintext=wipe)
    except RuntimeError as exc:
        typer.echo(f"  [x]  {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"  plaintext:  {result['plaintext_path']}")
    typer.echo(f"  encrypted:  {result['encrypted_path']}")
    typer.echo(f"  migrated:       {result['migrated']}")
    typer.echo(f"  identical:      {result['skipped_same']}")
    typer.echo(f"  conflicts:      {result['skipped_conflict']}")
    if result["conflicts"]:
        typer.echo("  conflicting names (unresolved — edit + retry):")
        for name in result["conflicts"]:
            typer.echo(f"    - {name}")
        raise typer.Exit(code=1)
    if result["wiped_plaintext"]:
        typer.echo("  [ok]  plaintext secrets.json removed")
    elif wipe and result["migrated"] == 0 and result["skipped_same"] == 0:
        typer.echo("  [ok]  nothing to migrate")
    elif not wipe:
        typer.echo("  [ok]  migration complete (plaintext kept; --wipe to remove)")
    else:
        typer.echo("  [ok]  migration complete")


# ── xmclaw config show ────────────────────────────────────────────────
# Epic #16 Phase 1 complement: read the daemon config and dump it with
# sensitive fields masked. Most users reach for ``cat daemon/config.json``
# today — that's fine alone but dangerous during screenshare / paste-into-
# chat. This command gives a safe-by-default view and an explicit
# ``--reveal`` for when full content is actually needed.
#
# Masking is *path-based* (key names match a denylist) rather than value-
# based (entropy / format sniffing) so it doesn't depend on the secret's
# shape — a custom-format self-hosted key stays masked too.

_SENSITIVE_KEY_SUFFIXES = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "access_key",
    "private_key",
)
"""Lowercase key-name suffixes whose values are masked by ``config show``.

Path-based matching: a key is sensitive when its *leaf* name ends in one
of these (case-insensitive). Intermediate nodes like ``auth`` are never
masked — otherwise you'd lose the structure preview that makes this
command useful."""


def _is_sensitive_key(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(suf) for suf in _SENSITIVE_KEY_SUFFIXES)


def _mask_value(val: Any) -> Any:
    """Render a sensitive value as a length-preserving hint.

    Keeps first/last 2 chars so the operator can still disambiguate
    "did I paste the right key" without leaking the full value. Short
    values (<=4 chars) collapse to all-stars so the prefix/suffix
    doesn't effectively reveal everything.
    """
    if val is None:
        return None
    if not isinstance(val, str):
        # Non-string sensitive values are rare (mostly numeric tokens).
        # Mask them wholesale — a partial reveal of a number is a bigger
        # leak than a string because the space is smaller.
        return "***"
    if val == "":
        return ""
    if len(val) <= 4:
        return "*" * len(val)
    return f"{val[:2]}{'*' * (len(val) - 4)}{val[-2:]}"


def _mask_config(obj: Any, *, path: tuple[str, ...] = ()) -> Any:
    """Walk a parsed config dict, masking sensitive leaves by key name.

    Args:
        obj: Node being walked. Dicts recurse; lists recurse element-wise
            with the parent key applied to each element (so a list of
            tokens is uniformly masked); scalars pass through unless
            the parent key flagged them sensitive.
        path: Dotted path of ancestor keys for debugging / future
            reference. Not used for masking decisions — only the
            immediate parent key is.
    """
    if isinstance(obj, dict):
        return {
            k: (
                _mask_value(v)
                if _is_sensitive_key(k) and not isinstance(v, (dict, list))
                else _mask_config(v, path=path + (k,))
            )
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        # List context inherits sensitivity from the parent key via the
        # caller's dict-level branch. When we hit a list here it means
        # the parent key wasn't sensitive, so recurse plain.
        return [_mask_config(v, path=path) for v in obj]
    return obj


@config_app.command("unset")
def config_unset(
    key: str = typer.Argument(
        ..., help="Dotted key path to remove, e.g. 'llm.anthropic.api_key'.",
    ),
    path: str = typer.Option(
        "daemon/config.json", "--path",
        help="Config file to mutate (default: daemon/config.json).",
    ),
    prune_empty: bool = typer.Option(
        False, "--prune-empty",
        help="After removing the leaf, drop parent dicts that became empty.",
    ),
) -> None:
    """Remove one dotted key from the config file (symmetric to ``set``).

    Exits 1 on: missing file / non-JSON / non-object root / key not set.
    A missing key is a hard error rather than a silent success — otherwise
    a typo in the key name would look like "done" and the value would
    linger.

    ``--prune-empty`` cascades up: if ``llm.anthropic.api_key`` was the
    only child of ``llm.anthropic``, the empty dict (and ``llm`` if it
    became empty) is also removed. Off by default because leaving the
    parent containers preserves the shape ``xmclaw config init`` wrote.
    """
    import json as _json

    target = _resolve_config_path(path)
    if not target.exists():
        typer.echo(
            f"  [x]  no config at {target} -- run 'xmclaw config init' first",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        data = _json.loads(target.read_text(encoding="utf-8"))
    except _json.JSONDecodeError as exc:
        typer.echo(f"  [x]  {target} is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1)
    if not isinstance(data, dict):
        typer.echo(
            f"  [x]  {target} must have a JSON object at its root, "
            f"got {type(data).__name__}",
            err=True,
        )
        raise typer.Exit(code=1)

    parts = [p for p in key.split(".") if p]
    if not parts:
        typer.echo("  [x]  key must be non-empty", err=True)
        raise typer.Exit(code=2)

    # Walk to the parent, collecting the chain so --prune-empty can
    # unwind. If any segment along the way is missing or non-dict, the
    # key simply isn't set — treat uniformly as not-found.
    chain: list[tuple[dict, str]] = []
    cursor: Any = data
    for segment in parts[:-1]:
        if not isinstance(cursor, dict) or segment not in cursor:
            typer.echo(f"  [x]  key not set: {key}", err=True)
            raise typer.Exit(code=1)
        chain.append((cursor, segment))
        cursor = cursor[segment]
    if not isinstance(cursor, dict) or parts[-1] not in cursor:
        typer.echo(f"  [x]  key not set: {key}", err=True)
        raise typer.Exit(code=1)

    del cursor[parts[-1]]

    if prune_empty:
        # Walk the chain in reverse, dropping containers that just became
        # empty. Stops the moment a parent still has siblings — we never
        # touch keys the user didn't ask about.
        for parent, seg in reversed(chain):
            child = parent[seg]
            if isinstance(child, dict) and not child:
                del parent[seg]
            else:
                break

    target.write_text(
        _json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    typer.echo(f"  [ok]  {target}: unset {key}")


_CONFIG_KEY_MISSING = object()
"""Sentinel — lets `_lookup_dotted` distinguish "key absent" from "value = None"."""


def _lookup_dotted(data: dict, key: str) -> Any:
    """Resolve ``a.b.c`` against a dict, returning ``_CONFIG_KEY_MISSING``
    when any segment is missing or isn't a dict.

    Matches :func:`config_set` dotted semantics: empty segments (``..``
    or leading ``.``) are ignored; a fully-empty key errors upstream.
    """
    parts = [p for p in key.split(".") if p]
    if not parts:
        return _CONFIG_KEY_MISSING
    cursor: Any = data
    for segment in parts:
        if not isinstance(cursor, dict) or segment not in cursor:
            return _CONFIG_KEY_MISSING
        cursor = cursor[segment]
    return cursor


@config_app.command("get")
def config_get(
    key: str = typer.Argument(
        ..., help="Dotted key path, e.g. 'gateway.port' or 'llm.anthropic.api_key'.",
    ),
    path: str = typer.Option(
        "daemon/config.json", "--path",
        help="Config file to read (default: daemon/config.json).",
    ),
    reveal: bool = typer.Option(
        False, "--reveal",
        help=(
            "Print the raw value for sensitive leaves (api_key / token / secret / "
            "password / etc). Default masks them so the output is safe to paste."
        ),
    ),
    json_output: bool = typer.Option(
        False, "--json",
        help="Emit the value as JSON (strings get quoted). Scripting-friendly.",
    ),
) -> None:
    """Read a single dotted key from the config file.

    Companion to ``config set`` — after ``config set gateway.port 9000`` you
    can confirm with ``config get gateway.port``. Prints just the value
    (no surrounding object) so shell pipelines are easy.

    Exits 1 when the file is missing, not valid JSON, or the key isn't set.
    Missing keys are a hard error rather than printing empty: the common
    case of ``config get | xargs ...`` would silently do the wrong thing
    against blank output.
    """
    import json as _json

    target = _resolve_config_path(path)
    if not target.exists():
        typer.echo(
            f"  [x]  no config at {target} -- run 'xmclaw config init' first",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        data = _json.loads(target.read_text(encoding="utf-8"))
    except _json.JSONDecodeError as exc:
        typer.echo(f"  [x]  {target} is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1)
    if not isinstance(data, dict):
        typer.echo(
            f"  [x]  {target} must have a JSON object at its root, "
            f"got {type(data).__name__}",
            err=True,
        )
        raise typer.Exit(code=1)

    parts = [p for p in key.split(".") if p]
    if not parts:
        typer.echo("  [x]  key must be non-empty", err=True)
        raise typer.Exit(code=2)

    value = _lookup_dotted(data, key)
    if value is _CONFIG_KEY_MISSING:
        typer.echo(f"  [x]  key not set: {key}", err=True)
        raise typer.Exit(code=1)

    leaf = parts[-1]
    rendered: Any
    if reveal or not _is_sensitive_key(leaf):
        rendered = value
    else:
        rendered = _mask_value(value)

    if json_output:
        typer.echo(_json.dumps(rendered, ensure_ascii=False))
    elif isinstance(rendered, str):
        # Plain string — emit bare so it can be used as `$(xmclaw config get ...)`.
        typer.echo(rendered)
    else:
        # Numbers / bools / null / containers get JSON-encoded even in text
        # mode — printing Python's `True` / `None` would surprise scripts.
        typer.echo(_json.dumps(rendered, ensure_ascii=False))


@config_app.command("show")
def config_show(
    path: str = typer.Option(
        "daemon/config.json", "--path",
        help="Config file to read (default: daemon/config.json).",
    ),
    reveal: bool = typer.Option(
        False, "--reveal",
        help=(
            "Print sensitive values in full. Default masks api_key / token "
            "/ secret / password fields so this command is safe to paste "
            "into a chat or run on a screenshare."
        ),
    ),
    json_output: bool = typer.Option(
        False, "--json",
        help="Emit JSON instead of indented text (for piping).",
    ),
) -> None:
    """Print daemon config with sensitive values masked by default.

    Exits 1 when the file is missing or not valid JSON (the daemon
    factory would also reject it — fail early, say why).
    """
    import json as _json

    target = _resolve_config_path(path)
    if not target.exists():
        typer.echo(
            f"  [x]  no config at {target} -- run 'xmclaw config init' first",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        raw = _json.loads(target.read_text(encoding="utf-8"))
    except _json.JSONDecodeError as exc:
        typer.echo(f"  [x]  {target} is not valid JSON: {exc}", err=True)
        raise typer.Exit(code=1)

    rendered = raw if reveal else _mask_config(raw)
    if json_output:
        typer.echo(_json.dumps(rendered, indent=2, ensure_ascii=False))
    else:
        typer.echo(f"  [ok]  {target}")
        typer.echo(_json.dumps(rendered, indent=2, ensure_ascii=False))


# ── xmclaw backup ──────────────────────────────────────────────────────
# Epic #20 entry. The CLI is a thin shell; all real work lives in
# xmclaw.backup so other frontends (future web UI, scheduled task) can
# reuse the same code path. Kept at the bottom so the rest of main.py's
# ordering isn't disturbed.


