"""Epic #16 Phase 1 — xmclaw.utils.secrets unit tests.

Covers the three-tier precedence (env > file > keyring) plus the CLI
shell. Keyring is stubbed out via monkeypatch so the tests run on any
machine including a bare CI worker with no D-Bus / Credential Manager.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from xmclaw.cli.main import app
from xmclaw.utils import secrets as secrets_mod


# ── helpers ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_secrets_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin secrets.json under tmp_path so tests can't touch the real
    ``~/.xmclaw/secrets.json``. The module respects XMC_SECRETS_PATH."""
    monkeypatch.setenv("XMC_SECRETS_PATH", str(tmp_path / "secrets.json"))
    # Also clear any XMC_SECRET_* env vars bleeding from the host.
    for key in list(os.environ):
        if key.startswith("XMC_SECRET_"):
            monkeypatch.delenv(key, raising=False)


class _FakeKeyring:
    """Minimal in-memory stand-in for the ``keyring`` module."""

    def __init__(self, initial: dict[tuple[str, str], str] | None = None):
        self._store: dict[tuple[str, str], str] = dict(initial or {})

    def get_password(self, service: str, name: str) -> str | None:
        return self._store.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self._store[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        if (service, name) not in self._store:
            raise RuntimeError("entry not found")
        del self._store[(service, name)]


def _force_keyring(
    monkeypatch: pytest.MonkeyPatch, module: _FakeKeyring | None,
) -> None:
    """Redirect ``_keyring_module`` so tests can control the keyring layer."""
    monkeypatch.setattr(secrets_mod, "_keyring_module", lambda: module)


# ── env_var_for normalization ───────────────────────────────────────────


def test_env_var_for_normalizes_punctuation() -> None:
    assert secrets_mod._env_var_for("llm.anthropic.api_key") == (
        "XMC_SECRET_LLM_ANTHROPIC_API_KEY"
    )
    assert secrets_mod._env_var_for("foo-bar") == "XMC_SECRET_FOO_BAR"
    assert secrets_mod._env_var_for("with spaces") == "XMC_SECRET_WITH_SPACES"


# ── secrets_file_path ───────────────────────────────────────────────────


def test_secrets_file_path_honors_env_override(tmp_path: Path) -> None:
    assert secrets_mod.secrets_file_path() == tmp_path / "secrets.json"


# ── file backend ────────────────────────────────────────────────────────


def test_set_then_get_secret_roundtrip() -> None:
    secrets_mod.set_secret("acme.key", "sk-abc")
    assert secrets_mod.get_secret("acme.key") == "sk-abc"


def test_set_overwrites_existing_entry() -> None:
    secrets_mod.set_secret("k", "v1")
    secrets_mod.set_secret("k", "v2")
    assert secrets_mod.get_secret("k") == "v2"


def test_get_secret_returns_none_when_unset() -> None:
    assert secrets_mod.get_secret("nonexistent") is None


def test_delete_secret_removes_from_file() -> None:
    secrets_mod.set_secret("k", "v")
    assert secrets_mod.delete_secret("k") is True
    assert secrets_mod.get_secret("k") is None


def test_delete_secret_returns_false_when_nothing_to_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_keyring(monkeypatch, None)
    assert secrets_mod.delete_secret("never_set") is False


def test_list_secret_names_reports_file_entries_sorted() -> None:
    secrets_mod.set_secret("zeta", "z")
    secrets_mod.set_secret("alpha", "a")
    assert secrets_mod.list_secret_names() == ["alpha", "zeta"]


def test_corrupt_secrets_file_is_treated_as_empty(tmp_path: Path) -> None:
    """A malformed secrets.json should NOT crash get_secret — prefer
    availability over strictness (doctor surfaces the typo later)."""
    (tmp_path / "secrets.json").write_text("{not json", encoding="utf-8")
    assert secrets_mod.get_secret("anything") is None
    assert secrets_mod.list_secret_names() == []


def test_non_dict_secrets_file_is_treated_as_empty(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text("[\"not a dict\"]", encoding="utf-8")
    assert secrets_mod.get_secret("x") is None


def test_file_write_uses_0600_on_posix(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("chmod semantics are POSIX-only")
    secrets_mod.set_secret("k", "v")
    path = tmp_path / "secrets.json"
    assert path.exists()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


# ── env precedence ──────────────────────────────────────────────────────


def test_env_wins_over_file(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_mod.set_secret("acme.key", "from-file")
    monkeypatch.setenv("XMC_SECRET_ACME_KEY", "from-env")
    assert secrets_mod.get_secret("acme.key") == "from-env"


def test_empty_env_var_falls_through_to_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whitespace-only ``XMC_SECRET_FOO=`` must NOT shadow the file
    layer — that's a common export-left-empty footgun."""
    secrets_mod.set_secret("acme.key", "from-file")
    monkeypatch.setenv("XMC_SECRET_ACME_KEY", "   ")
    assert secrets_mod.get_secret("acme.key") == "from-file"


def test_iter_env_override_names_reports_only_matching_file_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_mod.set_secret("overridden", "fv")
    secrets_mod.set_secret("not_overridden", "fv2")
    monkeypatch.setenv("XMC_SECRET_OVERRIDDEN", "ev")
    overrides = list(secrets_mod.iter_env_override_names())
    assert overrides == ["overridden"]


# ── keyring precedence ─────────────────────────────────────────────────


def test_file_wins_over_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKeyring({("xmclaw", "k"): "from-keyring"})
    _force_keyring(monkeypatch, fake)
    secrets_mod.set_secret("k", "from-file")
    assert secrets_mod.get_secret("k") == "from-file"


def test_keyring_used_when_file_and_env_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeKeyring({("xmclaw", "k"): "from-keyring"})
    _force_keyring(monkeypatch, fake)
    assert secrets_mod.get_secret("k") == "from-keyring"


def test_keyring_missing_module_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_keyring(monkeypatch, None)
    # Nothing else is set — should return None, not raise.
    assert secrets_mod.get_secret("k") is None


def test_keyring_exception_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Broken:
        def get_password(self, _service: str, _name: str) -> str | None:
            raise RuntimeError("D-Bus not running")

    _force_keyring(monkeypatch, _Broken())
    # Must not raise — secrets layer failing should never kill a caller.
    assert secrets_mod.get_secret("k") is None


def test_set_secret_keyring_backend_requires_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_keyring(monkeypatch, None)
    with pytest.raises(RuntimeError, match="keyring package not installed"):
        secrets_mod.set_secret("k", "v", backend="keyring")


def test_set_secret_keyring_backend_writes_to_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeKeyring()
    _force_keyring(monkeypatch, fake)
    secrets_mod.set_secret("k", "v", backend="keyring")
    assert fake.get_password("xmclaw", "k") == "v"


def test_delete_secret_clears_keyring_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeKeyring({("xmclaw", "k"): "val"})
    _force_keyring(monkeypatch, fake)
    secrets_mod.set_secret("k", "also_file")
    assert secrets_mod.delete_secret("k") is True
    assert secrets_mod.get_secret("k") is None


# ── CLI ─────────────────────────────────────────────────────────────────


def test_cli_set_secret_via_value_flag() -> None:
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["config", "set-secret", "acme.k", "--value", "sk-123"],
    )
    assert r.exit_code == 0, r.stdout
    assert secrets_mod.get_secret("acme.k") == "sk-123"
    assert "stored" in r.stdout


def test_cli_set_secret_reads_stdin_when_value_omitted() -> None:
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["config", "set-secret", "acme.k"],
        input="sk-stdin\n",
    )
    assert r.exit_code == 0, r.stdout
    assert secrets_mod.get_secret("acme.k") == "sk-stdin"


def test_cli_set_secret_refuses_empty_value() -> None:
    runner = CliRunner()
    r = runner.invoke(
        app,
        ["config", "set-secret", "acme.k", "--value", ""],
    )
    assert r.exit_code == 2


def test_cli_get_secret_masks_by_default() -> None:
    secrets_mod.set_secret("acme.k", "sk-1234567890")
    runner = CliRunner()
    r = runner.invoke(app, ["config", "get-secret", "acme.k"])
    assert r.exit_code == 0
    assert "sk-1234567890" not in r.stdout  # masked
    assert "len=13" in r.stdout


def test_cli_get_secret_reveal_prints_raw() -> None:
    secrets_mod.set_secret("acme.k", "sk-xyz")
    runner = CliRunner()
    r = runner.invoke(app, ["config", "get-secret", "acme.k", "--reveal"])
    assert r.exit_code == 0
    assert "sk-xyz" in r.stdout


def test_cli_get_secret_missing_exits_nonzero() -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["config", "get-secret", "never_set"])
    assert r.exit_code == 1
    assert "not set" in r.stdout


def test_cli_delete_secret_works() -> None:
    secrets_mod.set_secret("acme.k", "v")
    runner = CliRunner()
    r = runner.invoke(app, ["config", "delete-secret", "acme.k"])
    assert r.exit_code == 0
    assert secrets_mod.get_secret("acme.k") is None


def test_cli_list_secrets_empty_is_informative(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["config", "list-secrets"])
    assert r.exit_code == 0
    assert "no secrets" in r.stdout
    assert str(tmp_path / "secrets.json") in r.stdout


def test_cli_list_secrets_marks_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets_mod.set_secret("a", "x")
    secrets_mod.set_secret("b", "y")
    monkeypatch.setenv("XMC_SECRET_A", "env")
    runner = CliRunner()
    r = runner.invoke(app, ["config", "list-secrets"])
    assert r.exit_code == 0
    assert "a  (overridden by env)" in r.stdout
    assert "\n  b" in r.stdout or r.stdout.strip().endswith("b")


def test_secrets_file_json_is_stable_and_sorted(tmp_path: Path) -> None:
    """The file layer writes sort_keys=True + trailing newline — diff-
    friendly for operators who check it into their dotfiles repo."""
    secrets_mod.set_secret("z", "last")
    secrets_mod.set_secret("a", "first")
    raw = (tmp_path / "secrets.json").read_text(encoding="utf-8")
    assert raw.endswith("\n")
    parsed = json.loads(raw)
    assert list(parsed) == ["a", "z"]
