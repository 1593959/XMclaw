"""Local secrets lookup (Epic #16 Phase 1 — no cryptography yet).

Three-tier precedence, cheapest-to-most-persistent:

1. ``XMC_SECRET_<NAME>`` environment variable — highest, because CI
   secrets and container env are how production callers inject creds.
2. ``~/.xmclaw/secrets.json`` — a plain JSON dict chmod'd 0600 on POSIX.
   Manageable via ``xmclaw config set-secret`` / ``get-secret``. The
   file is **not** encrypted yet — Fernet + keyring arrive in Epic #16
   Phase 2. This is still a strict improvement over putting API keys
   straight in ``config.json`` because it's a separate, narrowly-
   permissioned file that callers can opt into.
3. OS keyring via the optional ``keyring`` package — soft-imported; if
   the dep isn't installed this layer is silently skipped. Works with
   Windows Credential Manager, macOS Keychain, and freedesktop Secret
   Service out of the box.

Storage split: writes go to layer 2 by default (portable, explicit,
test-friendly). The env-var layer is read-only — you can't set an env
var for the parent process from inside the daemon. The keyring layer
is write-capable but opt-in via ``set_secret(..., backend="keyring")``
because silently mixing backends confuses users when one machine has
keyring and another doesn't.

``XMC_SECRETS_PATH`` overrides the file location for tests and
multi-profile setups. The directory is created with 0700 on first
write so the chmod on the file is meaningful.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable, Literal

from xmclaw.utils.paths import data_dir

SECRETS_FILE_NAME = "secrets.json"
"""Default name of the on-disk secrets dict. Lives directly under
:func:`xmclaw.utils.paths.data_dir` (not inside ``v2/``) so a full
``v2/`` wipe doesn't destroy API keys."""

_KEYRING_SERVICE = "xmclaw"
"""Keyring service identifier. A service name collision would let a
malicious package fish our keys out; ``xmclaw`` is distinctive enough
that we don't namespace further."""


# Subset of characters allowed in an env-var name segment. We normalize
# aggressively so ``get_secret("llm.anthropic.api_key")`` maps to
# ``XMC_SECRET_LLM_ANTHROPIC_API_KEY`` predictably — operators will
# type the env var by hand and a typo because of stray punctuation is
# cruel.
_NAME_SANITIZE = re.compile(r"[^A-Z0-9_]")


def _env_var_for(name: str) -> str:
    """Map ``foo.bar-baz`` → ``XMC_SECRET_FOO_BAR_BAZ``."""
    return "XMC_SECRET_" + _NAME_SANITIZE.sub("_", name.upper())


def secrets_file_path() -> Path:
    """Where the on-disk secrets dict lives. Honors ``XMC_SECRETS_PATH``."""
    override = os.environ.get("XMC_SECRETS_PATH")
    if override:
        return Path(override)
    return data_dir() / SECRETS_FILE_NAME


def _load_file() -> dict[str, str]:
    """Read the on-disk dict. Missing file → ``{}``; corrupt JSON → ``{}``
    with a silent skip so one bad write doesn't brick lookups. We choose
    availability over strictness: a typo in the file should surface via
    ``xmclaw doctor`` rather than taking down ``xmclaw start``."""
    path = secrets_file_path()
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _write_file(data: dict[str, str]) -> None:
    """Atomic-ish write: dump to ``<path>.tmp`` then rename. chmod 0600
    on POSIX so the file isn't world-readable the moment it lands."""
    path = secrets_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            # Non-critical — the later rename still succeeds.
            pass
    os.replace(tmp, path)


def _keyring_module():
    """Soft import. Returns the ``keyring`` module or ``None``.

    The isolated import avoids paying keyring's startup cost (it
    probes the D-Bus / Credential Manager on import) for the common
    case where the user isn't using it.
    """
    try:
        import keyring  # type: ignore

        return keyring
    except ImportError:
        return None


def get_secret(name: str) -> str | None:
    """Resolve a secret by name.

    Lookup order: env → secrets.json → keyring. First hit wins; returns
    ``None`` only when *all three* layers miss. Whitespace-only values
    are treated as misses (common accident with ``export FOO=`` or an
    empty dict entry).

    The name is user-visible — keep it lowercase-dotted
    (``"llm.anthropic.api_key"``) so the corresponding env var and
    CLI command are predictable.
    """
    env_key = _env_var_for(name)
    from_env = os.environ.get(env_key)
    if from_env and from_env.strip():
        return from_env

    file_data = _load_file()
    from_file = file_data.get(name)
    if from_file and from_file.strip():
        return from_file

    kr = _keyring_module()
    if kr is not None:
        try:
            from_keyring = kr.get_password(_KEYRING_SERVICE, name)
        except Exception:
            # Keyring raises on misconfigured backends (no D-Bus, no
            # login keyring, etc). A secrets-layer failure must not
            # block the daemon — fall through to None.
            from_keyring = None
        if from_keyring and from_keyring.strip():
            return from_keyring

    return None


def set_secret(
    name: str,
    value: str,
    *,
    backend: Literal["file", "keyring"] = "file",
) -> None:
    """Write a secret to the chosen backend.

    Args:
        name: Secret name (same form used for :func:`get_secret`).
        value: Plaintext. Leading/trailing whitespace is preserved —
            copy-paste from a terminal sometimes includes meaningful
            dots/equals signs on the tail.
        backend: ``"file"`` (default) or ``"keyring"``. ``"keyring"``
            requires the optional ``keyring`` package and raises
            :class:`RuntimeError` when it's not installed.
    """
    if backend == "file":
        data = _load_file()
        data[name] = value
        _write_file(data)
        return
    if backend == "keyring":
        kr = _keyring_module()
        if kr is None:
            raise RuntimeError(
                "keyring package not installed; pip install keyring "
                "or call set_secret(..., backend='file')"
            )
        kr.set_password(_KEYRING_SERVICE, name, value)
        return
    raise ValueError(f"unknown backend: {backend!r}")  # pragma: no cover


def delete_secret(name: str) -> bool:
    """Remove a secret from every backend we can write to.

    Returns ``True`` if at least one backend had a value to delete.
    The env-var layer is read-only; we can't unset the user's shell.

    We try keyring even if it returns the value via :func:`get_secret`,
    because a leftover in a now-unused backend is a subtle footgun.
    """
    removed = False
    data = _load_file()
    if name in data:
        data.pop(name)
        _write_file(data)
        removed = True
    kr = _keyring_module()
    if kr is not None:
        try:
            kr.delete_password(_KEYRING_SERVICE, name)
            removed = True
        except Exception:
            # Missing entry raises PasswordDeleteError — that's the common
            # case and not actually a failure.
            pass
    return removed


def list_secret_names() -> list[str]:
    """Names of secrets in the file backend, sorted.

    Env-var secrets and keyring secrets are intentionally NOT listed:

    * Env: filtering ``os.environ`` for our prefix would leak unrelated
      ``XMC_SECRET_*`` vars users may have set deliberately.
    * Keyring: no portable enumeration API across backends.

    Operators who want a full audit can run ``env | grep XMC_SECRET_``
    themselves. The file dict is the only authoritative inventory.
    """
    return sorted(_load_file().keys())


def iter_env_override_names() -> Iterable[str]:
    """Names of secrets that would currently be overridden by env vars.

    Used by doctor to warn "you set ``XMC_SECRET_FOO`` — that wins over
    your ``secrets.json`` entry, is that what you meant?". We derive
    names from the file dict (the inventory source) rather than pattern-
    matching ``os.environ`` to avoid false positives from unrelated
    ``XMC_SECRET_*`` exports.
    """
    for name in list_secret_names():
        if os.environ.get(_env_var_for(name)):
            yield name
