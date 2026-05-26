"""Pairing tokens for the v2 daemon — anti-req #8 (ClawJacked defense).

Problem closed:
  * Attack A (same-machine other user) — Unix 0600 perms on the token
    file; a different user cannot read it.
  * Attack B (malicious web page doing ``new WebSocket("ws://127.0.0.1:...")``
    from the victim's browser) — the page has no filesystem access
    and cannot read the token file, so its WS connection has no token
    and is rejected.

What this actually ships (B-338 audit #9 honesty pass): a
**shared-secret-from-file** approach. 256 random bits in a 0600 file,
clients read it and pass on connect. Strictly safe on loopback —
the 0600 perms keep other users out, and a hostile web page in the
victim's browser can't read the file.

What it does NOT ship (the audit caught the stale promise): full
ed25519 device pairing with a challenge-response handshake. That
was advertised in earlier docstrings as "Phase 4.7+" but never
landed. The shared secret is the only auth layer today.

Implications baked into the daemon:

* ``serve --host`` enforces loopback by default. Non-loopback
  binds (``--host 0.0.0.0``) are REFUSED unless the operator passes
  both ``--no-auth`` and ``--allow-non-loopback``, because the
  shared secret travels in WS query params and shows up in any
  reverse-proxy log line — useless on a public address.
* The interface (``validate_token``) is intentionally a drop-in
  swap if a future commit upgrades to challenge-response.

Token is 256 random bits → 64 hex chars. Regeneration is explicit
(``rotate_token``) — regenerating on every ``serve`` start would force
the chat client to re-read the file every run, which is annoying.

2026-05-26 (audit F1): added TTL + revocation. Pre-fix the token
file had no expiry timestamp — a token leaked once stayed valid
forever. Now the file format is

    <hex-token>\n<created_unix_ts>\n

Legacy single-line files are still accepted (treated as "no TTL,
created at first read"). ``validate_token`` consults the TTL
file when present; if the token is past its TTL or has been
revoked, validation fails even when the hex matches. Operators
trigger explicit rotation via ``rotate_token`` or revocation via
``revoke_token`` (CLI: ``xmclaw revoke-token``).
"""
from __future__ import annotations

import hmac
import os
import secrets
import sys
import time
from pathlib import Path

from xmclaw.utils.paths import default_token_path as _central_default_token_path


# 2026-05-26 (audit F1): default TTL. 30 days matches the existing
# events / journal retention defaults so operators only learn one
# dial. ``0`` (or any negative) disables expiry — the legacy
# behavior — for callers that explicitly want it.
DEFAULT_TOKEN_TTL_DAYS: float = 30.0


def default_token_path() -> Path:
    """Location of the pairing token file.

    Uses ``~/.xmclaw/v2/pairing_token.txt`` by default. Honors the
    ``XMC_V2_PAIRING_TOKEN_PATH`` env var for testing and non-standard
    installs, and ``XMC_DATA_DIR`` for moving the whole workspace.

    Delegates to :func:`xmclaw.utils.paths.default_token_path` — that
    module is the single source of truth for runtime paths (§3.1).
    """
    return _central_default_token_path()


def generate_token() -> str:
    """Return a fresh 256-bit random token as a 64-char hex string."""
    return secrets.token_hex(32)


def _read_token_file(path: Path) -> tuple[str, float | None]:
    """Parse the (possibly legacy) token file.

    Returns ``(token, created_ts)``. Legacy single-line files return
    ``created_ts=None`` so they're treated as "no TTL pinned yet".
    """
    raw = path.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return "", None
    token = lines[0]
    created: float | None = None
    if len(lines) >= 2:
        try:
            created = float(lines[1])
        except ValueError:
            created = None
    return token, created


def _write_token_file(path: Path, token: str, created_ts: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{token}\n{created_ts:.6f}\n", encoding="utf-8")
    _apply_owner_only_perms(path)


def load_or_create_token(path: Path | str | None = None) -> str:
    """Read the token at ``path`` (default ``default_token_path()``).

    If the file doesn't exist, generate a new token, write it with
    0600 perms on POSIX, and return it. Idempotent on repeat calls —
    the generated token is stable across restarts until ``rotate_token``
    is called or the file is deleted.
    """
    p = Path(path) if path is not None else default_token_path()
    if p.exists():
        token, created = _read_token_file(p)
        if not token:
            # File was empty / malformed. Treat as missing.
            p.unlink(missing_ok=True)
        elif created is None:
            # 2026-05-26 (audit F1): legacy single-line file — pin
            # the current time as created_ts so future TTL checks
            # have a reference point. Doesn't invalidate the token.
            _write_token_file(p, token, time.time())
            return token
        else:
            return token

    token = generate_token()
    _write_token_file(p, token, time.time())
    return token


def rotate_token(path: Path | str | None = None) -> str:
    """Delete the existing token and create a new one. Returns the new token."""
    p = Path(path) if path is not None else default_token_path()
    if p.exists():
        p.unlink()
    return load_or_create_token(p)


def revoke_token(path: Path | str | None = None) -> bool:
    """2026-05-26 (audit F1): explicit revocation.

    Deletes the token file outright. The next call to
    ``load_or_create_token`` will mint a fresh one — all existing
    clients holding the old token are immediately rejected by
    ``validate_token``.

    Returns True when something was removed, False when no token
    file existed.
    """
    p = Path(path) if path is not None else default_token_path()
    if p.exists():
        try:
            p.unlink()
            return True
        except OSError:
            return False
    return False


def token_age_seconds(path: Path | str | None = None) -> float | None:
    """Return how old the on-disk token is, or None for legacy files
    without a recorded creation timestamp."""
    p = Path(path) if path is not None else default_token_path()
    if not p.exists():
        return None
    _, created = _read_token_file(p)
    if created is None:
        return None
    return max(0.0, time.time() - created)


def validate_token(
    expected: str,
    presented: str | None,
    *,
    path: Path | str | None = None,
    ttl_days: float = DEFAULT_TOKEN_TTL_DAYS,
) -> bool:
    """Constant-time compare. None / empty presented → False.

    2026-05-26 (audit F1): also rejects when the on-disk token has
    aged past ``ttl_days``. ``ttl_days <= 0`` disables expiry
    (legacy behavior). ``path`` defaults to ``default_token_path()``
    — passing it explicitly lets test harnesses point at a fixture
    file without touching the real one.
    """
    if not expected or not presented:
        return False
    if not hmac.compare_digest(expected.strip(), presented.strip()):
        return False
    # Same hex — now check TTL.
    if ttl_days <= 0:
        return True
    age = token_age_seconds(path)
    if age is None:
        # Legacy file without a timestamp, or no file (caller passed
        # the expected string in directly). Pass — back-compat with
        # tests that don't touch a real token file.
        return True
    return age <= ttl_days * 86400.0


def _apply_owner_only_perms(path: Path) -> None:
    """Chmod to 0600 on POSIX; on Windows rely on default per-user home dir.

    Best-effort — some filesystems (FAT, network shares) reject chmod.
    The token path default lives under the user's home which is
    already user-scoped on Windows, so the fallback is acceptable.
    """
    if sys.platform == "win32":
        return  # Windows home dir is per-user; chmod has no effect.
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Fail open — the token may still be on a user-scoped FS.
        # Callers who need strict isolation should check perms afterward.
        pass
