"""Unit tests for ``xmclaw version`` — text + --json contract.

``xmclaw version`` is the smallest CLI surface but also the one that
bug-reports and CI pipelines copy into support tickets and telemetry
most often. Locking the shape here means README snippets, bug-report
templates, and scripted `jq` extractors stay on a stable axis.

Covered:
  * Default text output is ``xmclaw v<version>\n`` — tight single line
    because shell users read it by eye.
  * ``--json`` output parses and includes the four fields documented in
    the CLI help: ``name`` / ``version`` / ``python`` / ``platform``.
  * ``name`` is always ``"xmclaw"`` and ``version`` matches
    ``xmclaw.__version__`` — the identity axis that scripts key off.
  * ``python`` is a plain dotted version (``sys.version.split()[0]``)
    without the compiler tag, so it parses with packaging.Version.
  * ``platform`` is a non-empty string (platform.platform()); exact
    value is host-dependent so we only assert shape, not content.
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from xmclaw import __version__
from xmclaw.cli.main import app


def test_version_text_mode_is_single_line():
    runner = CliRunner()
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0, r.stdout
    # exactly one newline-terminated line; no extra banner or prefix.
    assert r.stdout == f"xmclaw v{__version__}\n"


def test_version_json_mode_has_expected_keys():
    runner = CliRunner()
    r = runner.invoke(app, ["version", "--json"])
    assert r.exit_code == 0, r.stdout
    payload = json.loads(r.stdout)
    # Lock the key set — adding a field should be a conscious change
    # (callers relying on this shape don't want silent drift).
    assert set(payload.keys()) == {"name", "version", "python", "platform"}


def test_version_json_mode_identity_axis_matches_package():
    """name is the constant `xmclaw` and version mirrors `xmclaw.__version__`
    — together these are what dist channels / bug-trackers key off."""
    runner = CliRunner()
    r = runner.invoke(app, ["version", "--json"])
    payload = json.loads(r.stdout)
    assert payload["name"] == "xmclaw"
    assert payload["version"] == __version__


def test_version_json_mode_python_is_plain_dotted_version():
    """`python` must be the `X.Y.Z` form only — no ``| (main, ...) [GCC ...]``
    build-tag trailer, so it parses with ``packaging.version.Version``."""
    import re

    runner = CliRunner()
    r = runner.invoke(app, ["version", "--json"])
    payload = json.loads(r.stdout)
    # Permissive: support 3-tuple and 4-tuple (e.g., "3.10.20" or "3.12.0rc1").
    assert re.match(r"^\d+\.\d+\.\d+", payload["python"]), payload["python"]
    assert " " not in payload["python"]  # no compiler banner


def test_version_json_mode_platform_is_nonempty_string():
    """Exact platform string varies (Windows-10-10.0.19045-SP0 /
    Linux-6.1.0-... etc) — assert only that it's a non-empty string so
    bug reports have something to key on."""
    runner = CliRunner()
    r = runner.invoke(app, ["version", "--json"])
    payload = json.loads(r.stdout)
    assert isinstance(payload["platform"], str)
    assert payload["platform"]  # non-empty
