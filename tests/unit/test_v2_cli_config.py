"""``xmclaw config init`` / ``config set`` — README-promised commands.

These used to exist only in docs; this suite pins the real implementation.
Contract:

* ``init`` writes a minimal-but-bootable JSON skeleton; refuses to
  overwrite unless ``--force``; accepts a non-interactive
  ``--provider``/``--api-key`` pair for scripted installs.
* ``set`` takes a dotted key and a value (JSON literal when parseable,
  else string); creates intermediate dicts; refuses to touch a missing
  file or a non-object root (the daemon factory only accepts dicts).
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from xmclaw.cli.main import app


# ── config init ─────────────────────────────────────────────────────────


def test_config_init_writes_skeleton(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "daemon" / "config.json"
    result = runner.invoke(app, ["config", "init", "--path", str(target)])
    assert result.exit_code == 0, result.stdout
    assert target.exists()
    data = json.loads(target.read_text(encoding="utf-8"))
    # Minimum viable boot shape.
    assert data["llm"]["default_provider"] == "anthropic"
    assert data["llm"]["anthropic"]["api_key"] == ""
    assert data["llm"]["openai"]["api_key"] == ""
    assert data["gateway"] == {"host": "127.0.0.1", "port": 8765}
    assert data["security"]["prompt_injection"] == "detect_only"


def test_config_init_refuses_to_overwrite(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    target.write_text('{"pre": "existing"}', encoding="utf-8")

    result = runner.invoke(app, ["config", "init", "--path", str(target)])
    assert result.exit_code != 0
    # File must be untouched.
    assert json.loads(target.read_text(encoding="utf-8")) == {"pre": "existing"}


def test_config_init_force_overwrites(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    target.write_text('{"stale": true}', encoding="utf-8")

    result = runner.invoke(
        app, ["config", "init", "--path", str(target), "--force"],
    )
    assert result.exit_code == 0, result.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert "stale" not in data
    assert "llm" in data


def test_config_init_populates_provider_and_api_key(tmp_path: Path) -> None:
    """Non-interactive install: CI / Docker want one-shot configuration."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    result = runner.invoke(app, [
        "config", "init",
        "--path", str(target),
        "--provider", "openai",
        "--api-key", "sk-test-key",
    ])
    assert result.exit_code == 0, result.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["llm"]["default_provider"] == "openai"
    assert data["llm"]["openai"]["api_key"] == "sk-test-key"
    # The other provider stays blank.
    assert data["llm"]["anthropic"]["api_key"] == ""


def test_config_init_rejects_unknown_provider(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    result = runner.invoke(app, [
        "config", "init",
        "--path", str(target),
        "--provider", "gemini",
    ])
    assert result.exit_code != 0
    # No file should have been written.
    assert not target.exists()


def test_config_init_creates_parent_directory(tmp_path: Path) -> None:
    """``daemon/config.json`` default wants mkdir -p semantics."""
    runner = CliRunner()
    target = tmp_path / "nested" / "path" / "config.json"
    result = runner.invoke(app, ["config", "init", "--path", str(target)])
    assert result.exit_code == 0, result.stdout
    assert target.exists()


# ── config set ──────────────────────────────────────────────────────────


def _seed(tmp_path: Path) -> Path:
    target = tmp_path / "config.json"
    target.write_text(json.dumps({
        "llm": {
            "default_provider": "anthropic",
            "anthropic": {"api_key": ""},
        },
        "gateway": {"host": "127.0.0.1", "port": 8765},
    }, indent=2), encoding="utf-8")
    return target


def test_config_set_updates_existing_key(tmp_path: Path) -> None:
    runner = CliRunner()
    target = _seed(tmp_path)
    result = runner.invoke(app, [
        "config", "set",
        "llm.anthropic.api_key", "sk-ant-xyz",
        "--path", str(target),
    ])
    assert result.exit_code == 0, result.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["llm"]["anthropic"]["api_key"] == "sk-ant-xyz"


def test_config_set_parses_json_literals(tmp_path: Path) -> None:
    """Integers, bools, lists must round-trip as JSON, not as strings."""
    runner = CliRunner()
    target = _seed(tmp_path)
    # integer
    r1 = runner.invoke(app, [
        "config", "set", "gateway.port", "9000", "--path", str(target),
    ])
    assert r1.exit_code == 0, r1.stdout
    # boolean
    r2 = runner.invoke(app, [
        "config", "set", "evolution.enabled", "true", "--path", str(target),
    ])
    assert r2.exit_code == 0, r2.stdout
    # list
    r3 = runner.invoke(app, [
        "config", "set", "tools.allowed_dirs", '["."]', "--path", str(target),
    ])
    assert r3.exit_code == 0, r3.stdout

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["gateway"]["port"] == 9000
    assert data["evolution"]["enabled"] is True
    assert data["tools"]["allowed_dirs"] == ["."]


def test_config_set_creates_intermediate_dicts(tmp_path: Path) -> None:
    """Dotted paths with missing prefixes auto-create the nested objects."""
    runner = CliRunner()
    target = _seed(tmp_path)
    result = runner.invoke(app, [
        "config", "set",
        "integrations.slack.bot_token", "xoxb-secret",
        "--path", str(target),
    ])
    assert result.exit_code == 0, result.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["integrations"]["slack"]["bot_token"] == "xoxb-secret"


def test_config_set_errors_when_file_missing(tmp_path: Path) -> None:
    runner = CliRunner()
    missing = tmp_path / "never_initialized.json"
    result = runner.invoke(app, [
        "config", "set", "llm.anthropic.api_key", "k",
        "--path", str(missing),
    ])
    assert result.exit_code != 0
    assert "config init" in (result.stdout + result.stderr)


def test_config_set_rejects_non_object_root(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "array.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")
    result = runner.invoke(app, [
        "config", "set", "foo", "bar", "--path", str(target),
    ])
    assert result.exit_code != 0
    # Must not have mutated the file.
    assert json.loads(target.read_text(encoding="utf-8")) == [1, 2, 3]


def test_config_set_rejects_invalid_json(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "bad.json"
    target.write_text("{not valid", encoding="utf-8")
    result = runner.invoke(app, [
        "config", "set", "foo", "bar", "--path", str(target),
    ])
    assert result.exit_code != 0


def test_config_set_string_value_falls_back_when_not_json(tmp_path: Path) -> None:
    """Plain strings with no JSON interpretation land as strings -- e.g.
    api keys that don't quote-wrap on the shell."""
    runner = CliRunner()
    target = _seed(tmp_path)
    # An unquoted bareword is not valid JSON -> we accept it as a string.
    result = runner.invoke(app, [
        "config", "set", "llm.anthropic.api_key", "sk-ant-plain",
        "--path", str(target),
    ])
    assert result.exit_code == 0, result.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["llm"]["anthropic"]["api_key"] == "sk-ant-plain"


def test_config_init_then_set_end_to_end(tmp_path: Path) -> None:
    """The README flow: init -> set -> doctor-parseable."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    r1 = runner.invoke(app, ["config", "init", "--path", str(target)])
    assert r1.exit_code == 0, r1.stdout
    r2 = runner.invoke(app, [
        "config", "set", "llm.anthropic.api_key", "sk-ant-abc",
        "--path", str(target),
    ])
    assert r2.exit_code == 0, r2.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["llm"]["anthropic"]["api_key"] == "sk-ant-abc"


# ── config show ─────────────────────────────────────────────────────────


def _write_cfg(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_config_show_masks_api_key_by_default(tmp_path: Path) -> None:
    """Sensitive leaves are partially masked; structure is preserved."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {
        "llm": {"anthropic": {"api_key": "sk-ant-abcdef-1234567890"}},
    })
    r = runner.invoke(app, ["config", "show", "--path", str(target), "--json"])
    assert r.exit_code == 0, r.stdout
    # Strip the first "[ok]" line — wait, --json skips the "[ok]" header.
    rendered = json.loads(r.stdout)
    masked = rendered["llm"]["anthropic"]["api_key"]
    assert "sk-ant-abcdef-1234567890" not in r.stdout  # nothing leaked
    assert masked.startswith("sk")
    assert masked.endswith("90")
    assert "*" in masked
    assert len(masked) == len("sk-ant-abcdef-1234567890")


def test_config_show_masks_multiple_sensitive_suffixes(tmp_path: Path) -> None:
    """token / secret / password / private_key all trigger masking."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {
        "channels": {"slack": {"bot_token": "xoxb-super-secret-token"}},
        "backup": {"encryption_secret": "fernet-1234567890"},
        "db": {"password": "p@ssw0rd-dev"},
        "ssh": {"private_key": "----BEGIN RSA----xyz"},
        "plain": "public value",
    })
    r = runner.invoke(app, ["config", "show", "--path", str(target), "--json"])
    assert r.exit_code == 0
    out = json.loads(r.stdout)
    assert "xoxb-super-secret-token" not in r.stdout
    assert "fernet-1234567890" not in r.stdout
    assert "p@ssw0rd-dev" not in r.stdout
    assert "BEGIN RSA" not in r.stdout
    # Public field must pass through verbatim.
    assert out["plain"] == "public value"


def test_config_show_reveal_prints_raw(tmp_path: Path) -> None:
    """``--reveal`` is the explicit opt-in for unmasked output."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"llm": {"anthropic": {"api_key": "sk-ant-zzz"}}})
    r = runner.invoke(app, [
        "config", "show", "--path", str(target), "--json", "--reveal",
    ])
    assert r.exit_code == 0
    assert "sk-ant-zzz" in r.stdout


def test_config_show_short_value_stars_entirely(tmp_path: Path) -> None:
    """A <=4-char secret would effectively leak under prefix/suffix
    mask — collapse to all-stars instead."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"foo": {"token": "ab"}})
    r = runner.invoke(app, ["config", "show", "--path", str(target), "--json"])
    assert r.exit_code == 0
    out = json.loads(r.stdout)
    assert out["foo"]["token"] == "**"


def test_config_show_missing_file_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    r = runner.invoke(app, [
        "config", "show", "--path", str(tmp_path / "nope.json"),
    ])
    assert r.exit_code == 1


def test_config_show_invalid_json_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    target.write_text("{not json", encoding="utf-8")
    r = runner.invoke(app, ["config", "show", "--path", str(target)])
    assert r.exit_code == 1


def test_config_show_preserves_nested_structure(tmp_path: Path) -> None:
    """Masking is per-leaf — intermediate dicts / lists survive intact."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {
        "gateway": {"host": "127.0.0.1", "port": 8765},
        "security": {"prompt_injection": "detect_only"},
        "tools": {"allowed_dirs": [".", "~/work"]},
    })
    r = runner.invoke(app, ["config", "show", "--path", str(target), "--json"])
    out = json.loads(r.stdout)
    assert out["gateway"] == {"host": "127.0.0.1", "port": 8765}
    assert out["security"]["prompt_injection"] == "detect_only"
    assert out["tools"]["allowed_dirs"] == [".", "~/work"]


def test_config_show_text_mode_is_human_readable(tmp_path: Path) -> None:
    """Default text mode includes the file path + indented JSON body."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"llm": {"anthropic": {"api_key": "sk-abcdef"}}})
    r = runner.invoke(app, ["config", "show", "--path", str(target)])
    assert r.exit_code == 0
    assert str(target) in r.stdout
    # Body is indented JSON — we should see structure, not a one-liner.
    assert "\n" in r.stdout.strip()


def test_config_show_case_insensitive_suffix_match(tmp_path: Path) -> None:
    """Key matching ignores case — ``apiKey`` masks like ``api_key``."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"prov": {"apiKey": "sk-mixed-case-0123"}})
    r = runner.invoke(app, ["config", "show", "--path", str(target), "--json"])
    out = json.loads(r.stdout)
    assert "sk-mixed-case-0123" not in r.stdout
    assert out["prov"]["apiKey"].startswith("sk")
    assert out["prov"]["apiKey"].endswith("23")


# ── config get ──────────────────────────────────────────────────────────


def test_config_get_returns_scalar(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"gateway": {"host": "127.0.0.1", "port": 9000}})
    r = runner.invoke(app, ["config", "get", "gateway.port", "--path", str(target)])
    assert r.exit_code == 0, r.stdout
    assert r.stdout.strip() == "9000"


def test_config_get_returns_string_bare(tmp_path: Path) -> None:
    """Plain strings come out unquoted so ``$(xmclaw config get ...)`` works."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"gateway": {"host": "0.0.0.0"}})
    r = runner.invoke(app, ["config", "get", "gateway.host", "--path", str(target)])
    assert r.exit_code == 0, r.stdout
    assert r.stdout.strip() == "0.0.0.0"
    assert '"' not in r.stdout.strip()


def test_config_get_masks_sensitive_by_default(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"llm": {"anthropic": {"api_key": "sk-ant-abcdef1234567890"}}})
    r = runner.invoke(
        app, ["config", "get", "llm.anthropic.api_key", "--path", str(target)]
    )
    assert r.exit_code == 0, r.stdout
    assert "abcdef" not in r.stdout
    assert "sk" in r.stdout and "90" in r.stdout
    assert "*" in r.stdout


def test_config_get_reveal_shows_raw(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"llm": {"anthropic": {"api_key": "sk-ant-abcdef"}}})
    r = runner.invoke(
        app,
        ["config", "get", "llm.anthropic.api_key", "--path", str(target), "--reveal"],
    )
    assert r.exit_code == 0, r.stdout
    assert "sk-ant-abcdef" in r.stdout


def test_config_get_json_mode_encodes_types(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {
        "evolution": {"enabled": True},
        "tools": {"allowed_dirs": ["/tmp/a", "/tmp/b"]},
        "gateway": {"host": "localhost"},
    })
    # bool → JSON "true"
    r = runner.invoke(
        app, ["config", "get", "evolution.enabled", "--path", str(target), "--json"]
    )
    assert r.exit_code == 0 and r.stdout.strip() == "true"
    # list → JSON array
    r = runner.invoke(
        app, ["config", "get", "tools.allowed_dirs", "--path", str(target), "--json"]
    )
    assert r.exit_code == 0
    assert json.loads(r.stdout) == ["/tmp/a", "/tmp/b"]
    # string → JSON-quoted
    r = runner.invoke(
        app, ["config", "get", "gateway.host", "--path", str(target), "--json"]
    )
    assert r.exit_code == 0 and r.stdout.strip() == '"localhost"'


def test_config_get_non_string_scalar_still_json(tmp_path: Path) -> None:
    """Even in text mode, bools/numbers go through json.dumps so scripts parse cleanly."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"evolution": {"enabled": False}})
    r = runner.invoke(
        app, ["config", "get", "evolution.enabled", "--path", str(target)]
    )
    assert r.exit_code == 0
    assert r.stdout.strip() == "false"  # NOT "False" (Python repr)


def test_config_get_missing_key_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"gateway": {"host": "x"}})
    r = runner.invoke(app, ["config", "get", "gateway.nope", "--path", str(target)])
    assert r.exit_code == 1
    combined = (r.stdout or "") + (r.stderr or "")
    assert "not set" in combined.lower() or "gateway.nope" in combined


def test_config_get_missing_nested_segment_exits_nonzero(tmp_path: Path) -> None:
    """Navigation through a non-dict value is also 'missing'."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"gateway": {"host": "x"}})
    r = runner.invoke(
        app, ["config", "get", "gateway.host.deeper", "--path", str(target)]
    )
    assert r.exit_code == 1


def test_config_get_missing_file_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"  # not created
    r = runner.invoke(app, ["config", "get", "gateway.port", "--path", str(target)])
    assert r.exit_code == 1


def test_config_get_invalid_json_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    target.write_text("{not json", encoding="utf-8")
    r = runner.invoke(app, ["config", "get", "gateway.port", "--path", str(target)])
    assert r.exit_code == 1


def test_config_get_empty_key_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"x": 1})
    r = runner.invoke(app, ["config", "get", ".", "--path", str(target)])
    assert r.exit_code == 2


def test_config_get_reveal_on_non_sensitive_is_noop(tmp_path: Path) -> None:
    """--reveal on a plain key shouldn't change the output."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"gateway": {"port": 8765}})
    r_plain = runner.invoke(app, ["config", "get", "gateway.port", "--path", str(target)])
    r_reveal = runner.invoke(
        app, ["config", "get", "gateway.port", "--path", str(target), "--reveal"]
    )
    assert r_plain.stdout == r_reveal.stdout


def test_config_get_then_set_roundtrip(tmp_path: Path) -> None:
    """End-to-end: set a key, get it back, value matches."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {})
    r = runner.invoke(
        app, ["config", "set", "gateway.port", "9001", "--path", str(target)]
    )
    assert r.exit_code == 0, r.stdout
    r = runner.invoke(app, ["config", "get", "gateway.port", "--path", str(target)])
    assert r.exit_code == 0 and r.stdout.strip() == "9001"


# ── config unset ────────────────────────────────────────────────────────


def test_config_unset_removes_scalar(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"gateway": {"host": "x", "port": 9000}})
    r = runner.invoke(app, ["config", "unset", "gateway.port", "--path", str(target)])
    assert r.exit_code == 0, r.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"gateway": {"host": "x"}}


def test_config_unset_leaves_parent_dict_by_default(tmp_path: Path) -> None:
    """Without --prune-empty, removing the last child leaves {} behind."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"llm": {"anthropic": {"api_key": "sk"}}})
    r = runner.invoke(
        app, ["config", "unset", "llm.anthropic.api_key", "--path", str(target)]
    )
    assert r.exit_code == 0, r.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"llm": {"anthropic": {}}}


def test_config_unset_prune_empty_cascades(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"llm": {"anthropic": {"api_key": "sk"}}})
    r = runner.invoke(
        app,
        [
            "config", "unset", "llm.anthropic.api_key",
            "--path", str(target), "--prune-empty",
        ],
    )
    assert r.exit_code == 0, r.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {}


def test_config_unset_prune_empty_stops_at_non_empty_parent(tmp_path: Path) -> None:
    """Cascade must not remove a container that still has siblings."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {
        "llm": {
            "anthropic": {"api_key": "sk"},
            "openai": {"api_key": "other"},
        },
    })
    r = runner.invoke(
        app,
        [
            "config", "unset", "llm.anthropic.api_key",
            "--path", str(target), "--prune-empty",
        ],
    )
    assert r.exit_code == 0, r.stdout
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"llm": {"openai": {"api_key": "other"}}}


def test_config_unset_missing_key_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"gateway": {"host": "x"}})
    r = runner.invoke(app, ["config", "unset", "gateway.port", "--path", str(target)])
    assert r.exit_code == 1


def test_config_unset_missing_nested_segment_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"gateway": {"host": "x"}})
    r = runner.invoke(
        app, ["config", "unset", "llm.anthropic.api_key", "--path", str(target)]
    )
    assert r.exit_code == 1


def test_config_unset_through_non_dict_exits_nonzero(tmp_path: Path) -> None:
    """``gateway.host.deeper`` where host='x' walks through a scalar — treat as missing."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"gateway": {"host": "x"}})
    r = runner.invoke(
        app, ["config", "unset", "gateway.host.deeper", "--path", str(target)]
    )
    assert r.exit_code == 1


def test_config_unset_missing_file_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"  # not created
    r = runner.invoke(app, ["config", "unset", "x", "--path", str(target)])
    assert r.exit_code == 1


def test_config_unset_invalid_json_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    target.write_text("{garbage", encoding="utf-8")
    r = runner.invoke(app, ["config", "unset", "x", "--path", str(target)])
    assert r.exit_code == 1


def test_config_unset_non_object_root_exits_nonzero(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    target.write_text("[1,2,3]", encoding="utf-8")
    r = runner.invoke(app, ["config", "unset", "x", "--path", str(target)])
    assert r.exit_code == 1


def test_config_unset_empty_key_exits_with_code_2(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {"x": 1})
    r = runner.invoke(app, ["config", "unset", ".", "--path", str(target)])
    assert r.exit_code == 2


def test_config_set_get_unset_full_roundtrip(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {})
    assert runner.invoke(
        app, ["config", "set", "gateway.port", "9000", "--path", str(target)]
    ).exit_code == 0
    assert runner.invoke(
        app, ["config", "get", "gateway.port", "--path", str(target)]
    ).stdout.strip() == "9000"
    assert runner.invoke(
        app, ["config", "unset", "gateway.port", "--path", str(target)]
    ).exit_code == 0
    r = runner.invoke(app, ["config", "get", "gateway.port", "--path", str(target)])
    assert r.exit_code == 1  # now missing


def test_config_unset_preserves_sibling_values(tmp_path: Path) -> None:
    """Unsetting one key must not perturb other top-level keys."""
    runner = CliRunner()
    target = tmp_path / "config.json"
    _write_cfg(target, {
        "gateway": {"host": "127.0.0.1", "port": 9000},
        "tools": {"allowed_dirs": ["."]},
    })
    r = runner.invoke(app, ["config", "unset", "gateway.port", "--path", str(target)])
    assert r.exit_code == 0
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["tools"] == {"allowed_dirs": ["."]}
    assert data["gateway"] == {"host": "127.0.0.1"}
