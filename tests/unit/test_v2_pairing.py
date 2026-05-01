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
    assert p.read_text(encoding="utf-8").strip() == tok


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
    assert p.read_text(encoding="utf-8").strip() == b


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
