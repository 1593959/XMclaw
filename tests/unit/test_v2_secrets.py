"""Epic #16 Phase 1 + Phase 2 — xmclaw.utils.secrets unit tests.

Covers the four-tier precedence (env > encrypted > file > keyring) plus
the CLI shell. Keyring is stubbed out via monkeypatch so the tests run
on any machine including a bare CI worker with no D-Bus / Credential
Manager. The Fernet layer uses real ``cryptography`` (it's a base dep
from Phase 2 on) — if the import fails at collection, the Phase 2
tests skip with a clear reason so Phase 1 coverage still runs.
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
    """Pin both the plaintext and the encrypted stores under ``tmp_path``
    so tests can't touch ``~/.xmclaw/secrets.json`` or
    ``~/.xmclaw.secret/``. The module honors ``XMC_SECRETS_PATH`` (plain
    file) and ``XMC_SECRET_DIR`` (encrypted root)."""
    monkeypatch.setenv("XMC_SECRETS_PATH", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("XMC_SECRET_DIR", str(tmp_path / ".xmclaw.secret"))
    # Also clear any XMC_SECRET_* env vars bleeding from the host.
    # (Skip XMC_SECRET_DIR since we just set it.)
    for key in list(os.environ):
        if key.startswith("XMC_SECRET_") and key != "XMC_SECRET_DIR":
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
    # Phase 2 default is "encrypted"; keep the POSIX mode check on the
    # legacy file layer by opting in explicitly.
    secrets_mod.set_secret("k", "v", backend="file")
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
    friendly for operators who check it into their dotfiles repo.

    Phase 2 default is ``encrypted``; this test pins backend="file" on
    purpose to regression-guard the legacy write shape.
    """
    secrets_mod.set_secret("z", "last", backend="file")
    secrets_mod.set_secret("a", "first", backend="file")
    raw = (tmp_path / "secrets.json").read_text(encoding="utf-8")
    assert raw.endswith("\n")
    parsed = json.loads(raw)
    assert list(parsed) == ["a", "z"]


# ── Phase 2: Fernet encrypted backend ──────────────────────────────────

# Skip the whole Phase 2 block at collection time if cryptography is
# missing — it's a base dep, but we want Phase 1 coverage to still run
# on a stripped-down environment (e.g. a vendored install with no
# cryptography). The `is_encryption_available()` helper is the public
# feature gate doctor / CLI use too.
pytestmark_phase2 = pytest.mark.skipif(
    not secrets_mod.is_encryption_available(),
    reason="cryptography not importable; Phase 2 Fernet layer disabled",
)


@pytestmark_phase2
def test_is_encryption_available_reports_true_when_cryptography_present() -> None:
    assert secrets_mod.is_encryption_available() is True


@pytestmark_phase2
def test_encrypted_set_get_roundtrip(tmp_path: Path) -> None:
    secrets_mod.set_secret("api", "sk-encrypted-value", backend="encrypted")
    assert secrets_mod.get_secret("api") == "sk-encrypted-value"
    # Ciphertext must actually land on disk at the expected path.
    assert secrets_mod.encrypted_secrets_path().is_file()
    # The raw blob must NOT contain the plaintext.
    blob = secrets_mod.encrypted_secrets_path().read_bytes()
    assert b"sk-encrypted-value" not in blob


@pytestmark_phase2
def test_encrypted_default_backend_does_not_touch_plaintext_file(
    tmp_path: Path,
) -> None:
    """Regression guard for the Epic #16 Phase 2 exit criterion: a
    default ``set_secret`` must not write into ``~/.xmclaw/secrets.json``."""
    secrets_mod.set_secret("api", "sk-value")  # default == "encrypted"
    plaintext_path = tmp_path / "secrets.json"
    assert not plaintext_path.exists(), (
        "default backend silently wrote plaintext — Phase 2 contract broken"
    )
    assert secrets_mod.encrypted_secrets_path().is_file()


@pytestmark_phase2
def test_master_key_created_lazily_with_correct_shape(tmp_path: Path) -> None:
    """Key file materialises on first encrypted write, not on import."""
    assert not secrets_mod.master_key_path().exists()
    secrets_mod.set_secret("api", "v", backend="encrypted")
    assert secrets_mod.master_key_path().is_file()
    key = secrets_mod.master_key_path().read_bytes().strip()
    # Fernet keys are 44-byte urlsafe-base64 (32 bytes → 44 chars).
    assert len(key) == 44


@pytestmark_phase2
def test_master_key_persists_across_calls(tmp_path: Path) -> None:
    secrets_mod.set_secret("k1", "v1", backend="encrypted")
    first_key = secrets_mod.master_key_path().read_bytes()
    secrets_mod.set_secret("k2", "v2", backend="encrypted")
    second_key = secrets_mod.master_key_path().read_bytes()
    assert first_key == second_key  # must not rotate silently
    # Both values must decrypt fine with the same key.
    assert secrets_mod.get_secret("k1") == "v1"
    assert secrets_mod.get_secret("k2") == "v2"


@pytestmark_phase2
def test_secret_dir_chmod_is_700_on_posix(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("chmod semantics are POSIX-only")
    secrets_mod.set_secret("k", "v", backend="encrypted")
    dir_mode = secrets_mod.secret_dir().stat().st_mode & 0o777
    assert dir_mode == 0o700


@pytestmark_phase2
def test_master_key_file_chmod_is_600_on_posix(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("chmod semantics are POSIX-only")
    secrets_mod.set_secret("k", "v", backend="encrypted")
    mode = secrets_mod.master_key_path().stat().st_mode & 0o777
    assert mode == 0o600


@pytestmark_phase2
def test_encrypted_wins_over_plain_file() -> None:
    """Priority: env > encrypted > file > keyring. Same name, different
    values → encrypted wins because the post-migration world has the
    encrypted copy as the canonical source of truth."""
    secrets_mod.set_secret("api", "plaintext-val", backend="file")
    secrets_mod.set_secret("api", "encrypted-val", backend="encrypted")
    assert secrets_mod.get_secret("api") == "encrypted-val"


@pytestmark_phase2
def test_env_still_beats_encrypted(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets_mod.set_secret("api", "encrypted-val", backend="encrypted")
    monkeypatch.setenv("XMC_SECRET_API", "env-val")
    assert secrets_mod.get_secret("api") == "env-val"


@pytestmark_phase2
def test_corrupt_encrypted_blob_is_treated_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated / scrambled ciphertext must not raise — doctor
    surfaces the breakage, lookups fall through to other layers."""
    secrets_mod.set_secret("api", "val", backend="encrypted")
    # Replace the blob with garbage while keeping a valid master key.
    secrets_mod.encrypted_secrets_path().write_bytes(b"not-a-fernet-blob")
    assert secrets_mod.get_secret("api") is None


@pytestmark_phase2
def test_corrupt_master_key_is_treated_as_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A damaged key file disables the encrypted layer rather than
    clobbering the live key (which would invalidate every ciphertext)."""
    secrets_mod.set_secret("api", "val", backend="encrypted")
    secrets_mod.master_key_path().write_bytes(b"not-a-valid-fernet-key")
    # get_secret falls through; no crash.
    assert secrets_mod.get_secret("api") is None


@pytestmark_phase2
def test_delete_secret_removes_from_encrypted_store() -> None:
    secrets_mod.set_secret("api", "v", backend="encrypted")
    assert secrets_mod.delete_secret("api") is True
    assert secrets_mod.get_secret("api") is None


@pytestmark_phase2
def test_list_secret_names_merges_layers() -> None:
    secrets_mod.set_secret("enc_only", "e", backend="encrypted")
    secrets_mod.set_secret("file_only", "f", backend="file")
    names = secrets_mod.list_secret_names()
    assert names == ["enc_only", "file_only"]


# ── Phase 2: migrate_plaintext_to_encrypted ─────────────────────────────


@pytestmark_phase2
def test_migrate_moves_plaintext_entries(tmp_path: Path) -> None:
    secrets_mod.set_secret("a", "va", backend="file")
    secrets_mod.set_secret("b", "vb", backend="file")
    result = secrets_mod.migrate_plaintext_to_encrypted()
    assert result["migrated"] == 2
    assert result["skipped_same"] == 0
    assert result["conflicts"] == []
    assert result["wiped_plaintext"] is True
    assert not (tmp_path / "secrets.json").exists()
    # Values now live in the encrypted store.
    assert secrets_mod.get_secret("a") == "va"
    assert secrets_mod.get_secret("b") == "vb"


@pytestmark_phase2
def test_migrate_is_idempotent(tmp_path: Path) -> None:
    secrets_mod.set_secret("a", "va", backend="file")
    first = secrets_mod.migrate_plaintext_to_encrypted()
    assert first["migrated"] == 1
    # Second call on empty plaintext is a no-op.
    second = secrets_mod.migrate_plaintext_to_encrypted()
    assert second["migrated"] == 0
    assert second["skipped_same"] == 0
    assert second["wiped_plaintext"] is False  # nothing to wipe


@pytestmark_phase2
def test_migrate_detects_conflict_and_blocks_wipe(tmp_path: Path) -> None:
    secrets_mod.set_secret("a", "encrypted-val", backend="encrypted")
    secrets_mod.set_secret("a", "plaintext-val", backend="file")
    result = secrets_mod.migrate_plaintext_to_encrypted()
    assert result["conflicts"] == ["a"]
    assert result["migrated"] == 0
    # Conflict must not silently win — both values remain in place.
    assert result["wiped_plaintext"] is False
    assert (tmp_path / "secrets.json").is_file()
    # And the encrypted value is still retrievable (precedence rule).
    assert secrets_mod.get_secret("a") == "encrypted-val"


@pytestmark_phase2
def test_migrate_skips_identical_entries(tmp_path: Path) -> None:
    secrets_mod.set_secret("a", "same", backend="encrypted")
    secrets_mod.set_secret("a", "same", backend="file")
    result = secrets_mod.migrate_plaintext_to_encrypted()
    assert result["migrated"] == 0
    assert result["skipped_same"] == 1
    assert result["conflicts"] == []
    # No-new-data migrations still get to wipe the redundant plaintext.
    assert result["wiped_plaintext"] is True
    assert not (tmp_path / "secrets.json").exists()


@pytestmark_phase2
def test_migrate_honors_no_wipe(tmp_path: Path) -> None:
    secrets_mod.set_secret("a", "va", backend="file")
    result = secrets_mod.migrate_plaintext_to_encrypted(wipe_plaintext=False)
    assert result["migrated"] == 1
    assert result["wiped_plaintext"] is False
    assert (tmp_path / "secrets.json").is_file()
    # Encrypted copy is still good.
    assert secrets_mod.get_secret("a") == "va"


@pytestmark_phase2
def test_migrate_raises_when_cryptography_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secrets_mod, "_fernet_module", lambda: None)
    secrets_mod._write_file({"a": "va"})
    with pytest.raises(RuntimeError, match="cryptography"):
        secrets_mod.migrate_plaintext_to_encrypted()


# ── Phase 2: CLI ────────────────────────────────────────────────────────


@pytestmark_phase2
def test_cli_migrate_secrets_happy_path(tmp_path: Path) -> None:
    secrets_mod.set_secret("a", "va", backend="file")
    runner = CliRunner()
    r = runner.invoke(app, ["config", "migrate-secrets"])
    assert r.exit_code == 0, r.stdout
    assert "migrated:       1" in r.stdout
    assert "plaintext secrets.json removed" in r.stdout
    assert not (tmp_path / "secrets.json").exists()


@pytestmark_phase2
def test_cli_migrate_secrets_dry_run_does_not_write(tmp_path: Path) -> None:
    secrets_mod.set_secret("a", "va", backend="file")
    runner = CliRunner()
    r = runner.invoke(app, ["config", "migrate-secrets", "--dry-run"])
    assert r.exit_code == 0, r.stdout
    assert "would migrate:         1" in r.stdout
    # Dry-run must not touch either store.
    assert (tmp_path / "secrets.json").is_file()
    assert not secrets_mod.encrypted_secrets_path().exists()


@pytestmark_phase2
def test_cli_migrate_secrets_conflict_exits_nonzero(tmp_path: Path) -> None:
    secrets_mod.set_secret("a", "encrypted-val", backend="encrypted")
    secrets_mod.set_secret("a", "plaintext-val", backend="file")
    runner = CliRunner()
    r = runner.invoke(app, ["config", "migrate-secrets"])
    assert r.exit_code == 1, r.stdout
    assert "conflict" in r.stdout.lower()
    assert "- a" in r.stdout


@pytestmark_phase2
def test_cli_migrate_secrets_no_wipe_keeps_plaintext(tmp_path: Path) -> None:
    secrets_mod.set_secret("a", "va", backend="file")
    runner = CliRunner()
    r = runner.invoke(app, ["config", "migrate-secrets", "--no-wipe"])
    assert r.exit_code == 0, r.stdout
    assert "migration complete (plaintext kept" in r.stdout
    assert (tmp_path / "secrets.json").is_file()


@pytestmark_phase2
def test_cli_migrate_secrets_nothing_to_do(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(app, ["config", "migrate-secrets"])
    assert r.exit_code == 0, r.stdout
    assert "nothing to migrate" in r.stdout


@pytestmark_phase2
def test_cli_migrate_secrets_errors_when_cryptography_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secrets_mod, "_fernet_module", lambda: None)
    # is_encryption_available routes through _fernet_module so this
    # also flips the CLI's precondition check.
    #
    # Click 8.3 removed the ``mix_stderr`` kwarg and always separates
    # streams — the diagnostic lands on ``result.stderr`` now.
    runner = CliRunner()
    r = runner.invoke(app, ["config", "migrate-secrets"])
    assert r.exit_code == 1
    combined = (r.stdout or "") + (getattr(r, "stderr", "") or "")
    assert "cryptography" in combined
