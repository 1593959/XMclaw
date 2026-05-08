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
"""
from __future__ import annotations

import hmac
import os
import secrets
import sys
from pathlib import Path

from xmclaw.utils.paths import default_token_path as _central_default_token_path


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


def load_or_create_token(path: Path | str | None = None) -> str:
    """Read the token at ``path`` (default ``default_token_path()``).

    If the file doesn't exist, generate a new token, write it with
    0600 perms on POSIX, and return it. Idempotent on repeat calls —
    the generated token is stable across restarts until ``rotate_token``
    is called or the file is deleted.
    """
    p = Path(path) if path is not None else default_token_path()
    if p.exists():
        return p.read_text(encoding="utf-8").strip()

    token = generate_token()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token + "\n", encoding="utf-8")
    _apply_owner_only_perms(p)
    return token


def rotate_token(path: Path | str | None = None) -> str:
    """Delete the existing token and create a new one. Returns the new token."""
    p = Path(path) if path is not None else default_token_path()
    if p.exists():
        p.unlink()
    return load_or_create_token(p)


def validate_token(expected: str, presented: str | None) -> bool:
    """Constant-time compare. None / empty presented → False."""
    if not expected or not presented:
        return False
    # Both must be str; hmac.compare_digest handles the timing-safe check.
    return hmac.compare_digest(expected.strip(), presented.strip())


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
