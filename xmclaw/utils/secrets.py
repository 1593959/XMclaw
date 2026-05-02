"""Local secrets lookup (Epic #16 Phase 1 + Phase 2 Fernet-at-rest).

Four-tier precedence, cheapest-to-most-persistent:

1. ``XMC_SECRET_<NAME>`` environment variable — highest, because CI
   secrets and container env are how production callers inject creds.
2. **Encrypted** ``~/.xmclaw.secret/secrets.enc`` — Fernet-encrypted
   JSON dict keyed by a 32-byte master key at ``~/.xmclaw.secret/master.key``
   (chmod 0600 on POSIX, dir chmod 0700). New ``set_secret`` calls land
   here by default (Phase 2). The directory is a **sibling** of
   :func:`~xmclaw.utils.paths.data_dir` on purpose — see
   :func:`~xmclaw.utils.paths.secret_dir` for the workspace-wipe /
   backup-safety rationale.
3. ``~/.xmclaw/secrets.json`` — the Phase 1 plaintext dict, chmod 0600.
   Still **read** (no data loss for upgraders) but no longer written by
   default. ``xmclaw config migrate-secrets`` drains this file into the
   encrypted store and removes the plaintext copy. Collision rule: an
   entry present in both layers resolves to the **encrypted** value —
   it's newer (migration would have wiped plaintext on success) and
   downgrading precedence is the "least surprising" direction of drift.
4. OS keyring via the optional ``keyring`` package — soft-imported; if
   the dep isn't installed this layer is silently skipped. Works with
   Windows Credential Manager, macOS Keychain, and freedesktop Secret
   Service out of the box.

Storage split: writes go to layer 2 (encrypted) by default for Phase 2.
``backend="file"`` / ``backend="keyring"`` remain available. The env-var
layer is read-only — you can't set an env var for the parent process
from inside the daemon.

``XMC_SECRETS_PATH`` overrides the plaintext file location; ``XMC_SECRET_DIR``
overrides the encrypted store root. Both are honored by tests and
multi-profile setups. The directories are created with ``0o700`` on
first write so the chmod on the files is meaningful.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable, Literal

from xmclaw.utils.paths import data_dir, secret_dir

SECRETS_FILE_NAME = "secrets.json"
"""Default name of the Phase 1 plaintext on-disk secrets dict. Lives
directly under :func:`xmclaw.utils.paths.data_dir` (not inside ``v2/``)
so a full ``v2/`` wipe doesn't destroy API keys. Post Phase 2 this is a
read-mostly legacy layer — default writes land in the encrypted store."""

MASTER_KEY_FILE_NAME = "master.key"
"""Fernet master key — 44-byte urlsafe-base64 string stored under
:func:`xmclaw.utils.paths.secret_dir`. One file, one machine: the key
is generated on first encrypted write and never rotates automatically.
Key rotation is an out-of-scope follow-up (re-encrypt + swap) since
Phase 2's scope is "stop writing plaintext", not "full KMS"."""

ENCRYPTED_SECRETS_FILE_NAME = "secrets.enc"
"""Fernet ciphertext file — binary blob, sibling of ``master.key``. The
``.enc`` suffix is load-bearing: it's what a human scanning the secret
dir uses to tell "this is encrypted data, don't panic" from "this is
the key, DO panic if it's world-readable"."""

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
        import keyring

        return keyring
    except ImportError:
        return None


# ── Phase 2: Fernet encrypted store ─────────────────────────────────────


def master_key_path() -> Path:
    """Path to the Fernet master key inside :func:`secret_dir`."""
    return secret_dir() / MASTER_KEY_FILE_NAME


def encrypted_secrets_path() -> Path:
    """Path to the Fernet ciphertext file inside :func:`secret_dir`."""
    return secret_dir() / ENCRYPTED_SECRETS_FILE_NAME


def _ensure_secret_dir() -> Path:
    """Create :func:`secret_dir` with ``0o700`` if missing.

    Called on every encrypted write, not on import — a pristine fresh
    install shouldn't touch ``$HOME`` until the user actually stores
    something. Idempotent: the ``chmod`` always runs because an
    attacker could have loosened a pre-existing dir, but a failing
    chmod (filesystem without POSIX modes, Windows, etc.) is logged as
    a silent skip, not a hard error.
    """
    path = secret_dir()
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def _fernet_module():
    """Soft import for ``cryptography.fernet``.

    cryptography is a base dep (added in Phase 2), but we keep the
    import local + soft so:

    * The keyring / file layers still work on an install where
      ``cryptography`` got somehow uninstalled (`pip uninstall` by
      hand, minimal container image, etc). A broken crypto layer
      degrades to "can't decrypt, fall through to other layers"
      rather than bricking the entire ``get_secret`` call.
    * Startup stays cheap — the OpenSSL FFI binding is not tiny and
      we don't want every ``import xmclaw.utils.secrets`` to pay it.
    """
    try:
        from cryptography.fernet import Fernet

        return Fernet
    except ImportError:
        return None


def _load_or_create_master_key() -> bytes | None:
    """Return the 44-byte urlsafe-base64 Fernet key, creating it if absent.

    Returns ``None`` when ``cryptography`` is unavailable — the caller
    must treat that as "encrypted layer is disabled" and fall through.
    The key file is written atomically (tmp + rename) with ``0o600`` on
    POSIX. A corrupt / truncated key file also returns ``None`` and does
    not auto-regenerate — silently replacing a broken key would
    invalidate every ciphertext on disk.
    """
    fernet_cls = _fernet_module()
    if fernet_cls is None:
        return None

    path = master_key_path()
    if path.is_file():
        try:
            raw = path.read_bytes().strip()
        except OSError:
            return None
        # Validate shape before returning — a Fernet key is 44 bytes
        # urlsafe-base64. ``Fernet(...)`` raises on anything else; doing
        # the check here means the caller can trust the return value.
        try:
            fernet_cls(raw)
        except Exception:
            return None
        return raw

    # First use: generate + persist.
    key = fernet_cls.generate_key()
    _ensure_secret_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(key)
    if os.name == "posix":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)
    return key


def _load_encrypted() -> dict[str, str]:
    """Decrypt :func:`encrypted_secrets_path` to a plain dict.

    Availability-over-strictness (same philosophy as :func:`_load_file`):
    missing file → ``{}``; corrupt ciphertext → ``{}`` with silent skip.
    A broken encrypted store must NOT prevent ``get_secret`` from still
    finding the value in the env var or keyring layer. ``xmclaw doctor``
    surfaces the underlying breakage.
    """
    fernet_cls = _fernet_module()
    if fernet_cls is None:
        return {}
    path = encrypted_secrets_path()
    if not path.is_file():
        return {}
    key = _load_or_create_master_key()
    if key is None:
        return {}
    try:
        blob = path.read_bytes()
        plaintext = fernet_cls(key).decrypt(blob)
        data = json.loads(plaintext.decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _write_encrypted(data: dict[str, str]) -> None:
    """Fernet-encrypt ``data`` and atomically replace the ciphertext file.

    Raises :class:`RuntimeError` when cryptography isn't importable —
    unlike the read path (which can silently fall through to other
    layers), a requested write *must* land or the user loses data.
    """
    fernet_cls = _fernet_module()
    if fernet_cls is None:
        raise RuntimeError(
            "cryptography package not installed; pip install cryptography "
            "or call set_secret(..., backend='file')"
        )
    key = _load_or_create_master_key()
    if key is None:  # pragma: no cover — fernet_cls check above rules this out
        raise RuntimeError("failed to load or create the Fernet master key")
    plaintext = json.dumps(data, sort_keys=True).encode("utf-8")
    blob = fernet_cls(key).encrypt(plaintext)

    _ensure_secret_dir()
    path = encrypted_secrets_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(blob)
    if os.name == "posix":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def is_encryption_available() -> bool:
    """True iff the Fernet layer can round-trip on this install.

    Doctor / CLI callers use this to decide whether to advertise the
    encrypted backend. False means ``cryptography`` is not importable
    — operators get a clear "install cryptography" message instead of
    a silent fallback to plaintext writes.
    """
    return _fernet_module() is not None


# ── Public API ──────────────────────────────────────────────────────────


def get_secret(name: str) -> str | None:
    """Resolve a secret by name.

    Lookup order: env → encrypted → plaintext file → keyring. First hit
    wins; returns ``None`` only when *all four* layers miss.
    Whitespace-only values are treated as misses (common accident with
    ``export FOO=`` or an empty dict entry).

    The name is user-visible — keep it lowercase-dotted
    (``"llm.anthropic.api_key"``) so the corresponding env var and
    CLI command are predictable.
    """
    env_key = _env_var_for(name)
    from_env = os.environ.get(env_key)
    if from_env and from_env.strip():
        return from_env

    enc_data = _load_encrypted()
    from_enc = enc_data.get(name)
    if from_enc and from_enc.strip():
        return from_enc

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
    backend: Literal["encrypted", "file", "keyring"] = "encrypted",
) -> None:
    """Write a secret to the chosen backend.

    Args:
        name: Secret name (same form used for :func:`get_secret`).
        value: Plaintext. Leading/trailing whitespace is preserved —
            copy-paste from a terminal sometimes includes meaningful
            dots/equals signs on the tail.
        backend: ``"encrypted"`` (default, Phase 2 Fernet store),
            ``"file"`` (legacy plaintext — kept for ops tooling
            compatibility), or ``"keyring"``. ``"keyring"`` requires the
            optional ``keyring`` package and raises
            :class:`RuntimeError` when it's not installed; ``"encrypted"``
            requires ``cryptography`` and raises the same when missing.
    """
    if backend == "encrypted":
        data = _load_encrypted()
        data[name] = value
        _write_encrypted(data)
        return
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

    We try keyring even if the value came from :func:`get_secret`
    because a leftover in a now-unused backend is a subtle footgun.
    """
    removed = False

    enc = _load_encrypted()
    if name in enc:
        enc.pop(name)
        try:
            _write_encrypted(enc)
            removed = True
        except RuntimeError:
            # cryptography unavailable after it had been available —
            # leave the value in place rather than swallowing the error.
            raise

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
    """Names of secrets across the writable on-disk backends, sorted.

    Merges the encrypted store and the plaintext file. Env-var secrets
    and keyring secrets are intentionally NOT listed:

    * Env: filtering ``os.environ`` for our prefix would leak unrelated
      ``XMC_SECRET_*`` vars users may have set deliberately.
    * Keyring: no portable enumeration API across backends.

    Operators who want a full audit can run ``env | grep XMC_SECRET_``
    themselves. The file + encrypted dicts are the authoritative
    inventory.
    """
    merged: set[str] = set(_load_encrypted().keys()) | set(_load_file().keys())
    return sorted(merged)


def iter_env_override_names() -> Iterable[str]:
    """Names of secrets that would currently be overridden by env vars.

    Used by doctor to warn "you set ``XMC_SECRET_FOO`` — that wins over
    your ``secrets.json`` entry, is that what you meant?". We derive
    names from the disk-backed inventory rather than pattern-matching
    ``os.environ`` to avoid false positives from unrelated
    ``XMC_SECRET_*`` exports.
    """
    for name in list_secret_names():
        if os.environ.get(_env_var_for(name)):
            yield name


# ── Migration ───────────────────────────────────────────────────────────


def migrate_plaintext_to_encrypted(*, wipe_plaintext: bool = True) -> dict[str, object]:
    """Move every entry from ``secrets.json`` into the Fernet store.

    Idempotent: re-running after a completed migration is a no-op that
    reports ``migrated = 0``. The result dict describes exactly what
    happened so a CLI / doctor caller can surface it:

    * ``migrated`` — number of keys copied into the encrypted store.
    * ``skipped_same`` — number of keys already present in the
      encrypted store with identical values (collision-free merge).
    * ``skipped_conflict`` — number of keys present in both layers with
      *different* values. These are left untouched in **both** layers,
      and their names land in ``conflicts``. Refuses to silently pick
      a winner — operator runs ``set-secret`` or ``delete-secret``
      explicitly.
    * ``wiped_plaintext`` — ``True`` iff we removed the plaintext
      ``secrets.json`` at the end. Only happens when every non-conflict
      entry migrated successfully AND ``wipe_plaintext`` is true AND
      no conflicts remain.
    * ``conflicts`` — list of names with divergent values.
    * ``plaintext_path`` / ``encrypted_path`` — resolved absolute paths,
      so the CLI can echo them back unambiguously.

    Raises :class:`RuntimeError` when ``cryptography`` isn't importable,
    because doing nothing would leave the user believing they'd just
    secured their secrets.
    """
    if _fernet_module() is None:
        raise RuntimeError(
            "cryptography package not installed; cannot migrate. "
            "Run `pip install cryptography` and retry."
        )

    plaintext = _load_file()
    encrypted = _load_encrypted()

    migrated = 0
    skipped_same = 0
    conflicts: list[str] = []

    merged = dict(encrypted)
    for name, value in plaintext.items():
        existing = encrypted.get(name)
        if existing is None:
            merged[name] = value
            migrated += 1
        elif existing == value:
            skipped_same += 1
        else:
            conflicts.append(name)

    if migrated:
        _write_encrypted(merged)

    wiped_plaintext = False
    plaintext_file = secrets_file_path()
    # Only wipe when the plaintext dict's keys are fully accounted for
    # in the encrypted store (either migrated or exactly-matching) AND
    # the caller asked for it. Conflicts block the wipe so the operator
    # doesn't lose data they haven't reconciled.
    if (
        wipe_plaintext
        and not conflicts
        and plaintext  # nothing to wipe if the plaintext dict was already empty
        and plaintext_file.is_file()
    ):
        try:
            plaintext_file.unlink()
            wiped_plaintext = True
        except OSError:
            wiped_plaintext = False

    return {
        "migrated": migrated,
        "skipped_same": skipped_same,
        "skipped_conflict": len(conflicts),
        "wiped_plaintext": wiped_plaintext,
        "conflicts": conflicts,
        "plaintext_path": str(plaintext_file),
        "encrypted_path": str(encrypted_secrets_path()),
    }
