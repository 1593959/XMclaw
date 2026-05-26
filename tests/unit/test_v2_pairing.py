"""Pairing-token tests — anti-req #8 plumbing.

Covers the three concerns:
  * generation produces a high-entropy token
  * load_or_create is idempotent across calls
  * validation is constant-time AND rejects the obvious bad shapes
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from xmclaw.daemon.pairing import (
    default_token_path,
    generate_token,
    load_or_create_token,
    rotate_token,
    validate_token,
)


# ── generate ────────────────────────────────────────────────────────────


def test_generate_token_is_64_hex_chars() -> None:
    tok = generate_token()
    assert len(tok) == 64
    int(tok, 16)  # must parse as hex


def test_generate_token_is_unique_across_calls() -> None:
    tokens = {generate_token() for _ in range(100)}
    # 256-bit random — collision probability is ~0 for 100 samples.
    assert len(tokens) == 100


# ── load_or_create ─────────────────────────────────────────────────────


def test_load_or_create_creates_file_on_first_call(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "pair.txt"
    tok = load_or_create_token(p)
    assert p.exists()
    assert len(tok) == 64
    # 2026-05-26 (audit F1): file now has 2 lines (token + created_ts).
    # The token is line 1; line 2 is a unix timestamp.
    body = p.read_text(encoding="utf-8")
    assert body.splitlines()[0].strip() == tok


def test_load_or_create_is_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "pair.txt"
    a = load_or_create_token(p)
    b = load_or_create_token(p)
    assert a == b  # same token across calls


def test_load_or_create_creates_parent_directories(tmp_path: Path) -> None:
    p = tmp_path / "a" / "b" / "c" / "token.txt"
    assert not p.parent.exists()
    load_or_create_token(p)
    assert p.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only chmod")
def test_load_or_create_sets_0600_on_posix(tmp_path: Path) -> None:
    p = tmp_path / "pair.txt"
    load_or_create_token(p)
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


# ── rotate ──────────────────────────────────────────────────────────────


def test_rotate_produces_a_different_token(tmp_path: Path) -> None:
    p = tmp_path / "pair.txt"
    a = load_or_create_token(p)
    b = rotate_token(p)
    assert a != b
    # 2026-05-26 (audit F1): file now has 2 lines (token + created_ts).
    body = p.read_text(encoding="utf-8")
    assert body.splitlines()[0].strip() == b


def test_rotate_when_no_existing_token_still_creates_one(tmp_path: Path) -> None:
    p = tmp_path / "pair.txt"
    tok = rotate_token(p)
    assert p.exists()
    assert len(tok) == 64


# ── validate ────────────────────────────────────────────────────────────


def test_validate_happy_path() -> None:
    tok = generate_token()
    assert validate_token(tok, tok) is True


def test_validate_rejects_none_presented() -> None:
    assert validate_token("expected", None) is False


def test_validate_rejects_empty_presented() -> None:
    assert validate_token("expected", "") is False


def test_validate_rejects_empty_expected() -> None:
    """Guard: an unconfigured daemon (empty expected) must not accept
    any token, including empty. Otherwise auth_check silently degrades
    to 'anyone can connect' if the token file is wiped."""
    assert validate_token("", "anything") is False
    assert validate_token("", "") is False


def test_validate_rejects_almost_matching_token() -> None:
    tok = generate_token()
    off_by_one = tok[:-1] + ("0" if tok[-1] != "0" else "1")
    assert validate_token(tok, off_by_one) is False


def test_validate_strips_whitespace() -> None:
    """Tokens read from a file have a trailing newline; validate should
    still match. The file writer already appends '\\n' so this path is
    exercised in real use."""
    tok = generate_token()
    assert validate_token(tok, tok + "\n") is True
    assert validate_token(tok + "\n", tok) is True


# ── env override ────────────────────────────────────────────────────────


def test_env_var_overrides_default_token_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    override = tmp_path / "custom.txt"
    monkeypatch.setenv("XMC_V2_PAIRING_TOKEN_PATH", str(override))
    assert default_token_path() == override


def test_no_env_var_returns_home_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XMC_V2_PAIRING_TOKEN_PATH", raising=False)
    p = default_token_path()
    # Either $HOME or the Python Path.home() returns something;
    # in tests this may be the actual user home, so just assert the
    # suffix matches.
    assert p.name == "pairing_token.txt"
    assert "v2" in p.parts


# 2026-05-26 (audit F1): TTL + revoke


def test_token_age_seconds_reports_age(tmp_path: Path) -> None:
    from xmclaw.daemon.pairing import token_age_seconds
    p = tmp_path / "pair.txt"
    load_or_create_token(p)
    age = token_age_seconds(p)
    assert age is not None
    assert 0.0 <= age < 5.0  # just created — must be young


def test_token_age_seconds_none_for_missing_file(tmp_path: Path) -> None:
    from xmclaw.daemon.pairing import token_age_seconds
    assert token_age_seconds(tmp_path / "nope.txt") is None


def test_validate_rejects_expired_token(tmp_path: Path) -> None:
    """Token older than ttl_days must fail validate even when hex matches."""
    import time
    from xmclaw.daemon.pairing import validate_token, _write_token_file
    p = tmp_path / "pair.txt"
    tok = "0" * 64
    # Pin created_ts to 100 days ago.
    _write_token_file(p, tok, time.time() - 100 * 86400)
    # ttl_days = 30 → should reject.
    assert validate_token(tok, tok, path=p, ttl_days=30.0) is False
    # ttl_days = 0 (disable) → accept.
    assert validate_token(tok, tok, path=p, ttl_days=0.0) is True


def test_validate_accepts_fresh_token(tmp_path: Path) -> None:
    from xmclaw.daemon.pairing import validate_token
    p = tmp_path / "pair.txt"
    tok = load_or_create_token(p)
    assert validate_token(tok, tok, path=p, ttl_days=30.0) is True


def test_legacy_single_line_file_gets_timestamp(tmp_path: Path) -> None:
    """A legacy file containing only the hex token must keep working —
    load_or_create stamps the current time so future TTL checks have
    a reference point. The original token survives."""
    p = tmp_path / "pair.txt"
    legacy = "a" * 64
    p.write_text(legacy + "\n", encoding="utf-8")
    tok = load_or_create_token(p)
    assert tok == legacy
    body = p.read_text(encoding="utf-8")
    assert len(body.splitlines()) == 2  # token + ts


def test_revoke_token_deletes_file(tmp_path: Path) -> None:
    from xmclaw.daemon.pairing import revoke_token
    p = tmp_path / "pair.txt"
    load_or_create_token(p)
    assert p.exists()
    assert revoke_token(p) is True
    assert not p.exists()


def test_revoke_token_idempotent_when_missing(tmp_path: Path) -> None:
    from xmclaw.daemon.pairing import revoke_token
    assert revoke_token(tmp_path / "never_existed.txt") is False


# 2026-05-26 (hotfix): read_token canonical reader


def test_read_token_returns_only_hex(tmp_path: Path) -> None:
    """``read_token`` MUST return only the hex line of the F1
    2-line file, never the timestamp. Pre-fix the /api/v2/pair
    endpoint did ``read_text().strip()`` which left the embedded
    \n intact → UI sent ``hex\nts`` as the token → every page
    hit 401 → memory page showed "未启用"."""
    from xmclaw.daemon.pairing import read_token
    p = tmp_path / "pair.txt"
    # Write the canonical 2-line shape directly.
    p.write_text("a" * 64 + "\n1779770000.123\n", encoding="utf-8")
    tok = read_token(p)
    assert tok == "a" * 64
    assert "\n" not in tok
    assert "1779770000" not in tok


def test_read_token_handles_legacy_single_line(tmp_path: Path) -> None:
    """Pre-F1 files had no timestamp line."""
    from xmclaw.daemon.pairing import read_token
    p = tmp_path / "pair.txt"
    p.write_text("b" * 64 + "\n", encoding="utf-8")
    assert read_token(p) == "b" * 64


def test_read_token_missing_returns_none(tmp_path: Path) -> None:
    from xmclaw.daemon.pairing import read_token
    assert read_token(tmp_path / "nope.txt") is None


def test_read_token_empty_returns_none(tmp_path: Path) -> None:
    from xmclaw.daemon.pairing import read_token
    p = tmp_path / "pair.txt"
    p.write_text("\n\n", encoding="utf-8")
    assert read_token(p) is None
