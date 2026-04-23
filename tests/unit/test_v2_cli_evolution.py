"""Epic #4 Phase A — unit tests for ``xmclaw evolution show``.

Scope: the CLI surface that reads ``SkillRegistry`` JSONL history files
and prints a formatted timeline. Phase A deliberately does NOT exercise
the orchestrator / SKILL_PROMOTED event tap — that lands in Phase B.

What we cover here:
  * :func:`xmclaw.cli.evolution._parse_since` — the ``--since`` dialect
  * :func:`xmclaw.cli.evolution._fmt_record` — promote / rollback lines
  * :func:`xmclaw.cli.evolution.run_evolution_show` — end-to-end read
    from a temporary skills dir, with ``--since`` filtering
  * round-trip against the real ``SkillRegistry._persist`` format so the
    CLI stays compatible if we ever tweak the record schema

The CLI resolves its skills dir via
:func:`xmclaw.utils.paths.skills_dir`, which honors
``XMC_V2_SKILLS_DIR``. Tests set that env var via ``monkeypatch`` so
each run sees an isolated dir.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from xmclaw.cli.evolution import (
    _fmt_record,
    _parse_since,
    run_evolution_show,
)
from xmclaw.cli.main import app
from xmclaw.skills.registry import SkillRegistry


# ---------------------------------------------------------------------------
# _parse_since
# ---------------------------------------------------------------------------
class TestParseSince:
    def test_none_returns_none(self) -> None:
        assert _parse_since(None) is None

    def test_hours_suffix(self) -> None:
        now = time.time()
        parsed = _parse_since("24h")
        assert parsed is not None
        # 24h ago, allow 5s of clock drift for the test process itself.
        assert abs(parsed - (now - 24 * 3600)) < 5

    def test_days_suffix(self) -> None:
        now = time.time()
        parsed = _parse_since("7d")
        assert parsed is not None
        assert abs(parsed - (now - 7 * 86400)) < 5

    def test_bare_integer_is_hours(self) -> None:
        now = time.time()
        parsed = _parse_since("3")
        assert parsed is not None
        assert abs(parsed - (now - 3 * 3600)) < 5

    def test_garbage_returns_none(self) -> None:
        assert _parse_since("potato") is None


# ---------------------------------------------------------------------------
# _fmt_record
# ---------------------------------------------------------------------------
class TestFormatRecord:
    def test_promote_shows_score_from_mean_evidence(self) -> None:
        rec = {
            "kind": "promote",
            "skill_id": "demo",
            "from_version": 1,
            "to_version": 2,
            "ts": 1700000000.0,
            "evidence": ["plays=5", "mean=0.823", "gap=0.12"],
        }
        line = _fmt_record(rec)
        assert "[+]" in line
        assert "demo" in line
        assert "v1 " in line and " v2" in line
        assert "0.823" in line

    def test_rollback_includes_reason(self) -> None:
        rec = {
            "kind": "rollback",
            "skill_id": "demo",
            "from_version": 2,
            "to_version": 1,
            "ts": 1700000000.0,
            "reason": "grader flagged regressions",
        }
        line = _fmt_record(rec)
        assert "[-]" in line
        assert "grader flagged regressions" in line

    def test_promote_without_mean_evidence_has_no_score(self) -> None:
        rec = {
            "kind": "promote",
            "skill_id": "demo",
            "from_version": 1,
            "to_version": 2,
            "ts": 1700000000.0,
            "evidence": ["plays=5"],
        }
        line = _fmt_record(rec)
        assert "(score" not in line


# ---------------------------------------------------------------------------
# run_evolution_show
# ---------------------------------------------------------------------------
def _seed(history_dir: Path, skill_id: str, records: list[dict[str, object]]) -> None:
    path = history_dir / f"{skill_id}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


class TestRunEvolutionShow:
    def test_empty_dir_is_friendly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("XMC_V2_SKILLS_DIR", str(tmp_path))
        rc = run_evolution_show(None)
        assert rc == 0
        assert "No evolution events" in capsys.readouterr().out

    def test_prints_promotions_and_rollbacks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("XMC_V2_SKILLS_DIR", str(tmp_path))
        now = time.time()
        _seed(tmp_path, "demo", [
            {"kind": "promote", "skill_id": "demo", "from_version": 1, "to_version": 2,
             "ts": now - 3600, "evidence": ["mean=0.823"]},
            {"kind": "rollback", "skill_id": "demo", "from_version": 2, "to_version": 1,
             "ts": now - 1800, "reason": "grader flagged regressions"},
        ])
        rc = run_evolution_show(None)
        assert rc == 0
        out = capsys.readouterr().out
        assert "demo" in out
        assert "[+]" in out and "[-]" in out
        assert "0.823" in out
        assert "grader flagged regressions" in out

    def test_since_filters_old_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("XMC_V2_SKILLS_DIR", str(tmp_path))
        now = time.time()
        _seed(tmp_path, "demo", [
            # 48h ago — should be filtered out by --since 24h
            {"kind": "promote", "skill_id": "demo", "from_version": 1, "to_version": 2,
             "ts": now - 48 * 3600, "evidence": ["mean=0.7"]},
            # 1h ago — should survive
            {"kind": "promote", "skill_id": "demo", "from_version": 2, "to_version": 3,
             "ts": now - 3600, "evidence": ["mean=0.9"]},
        ])
        rc = run_evolution_show("24h")
        assert rc == 0
        out = capsys.readouterr().out
        assert "v2 " in out and " v3" in out
        assert "v1 " not in out or "v1 → v2" not in out

    def test_records_from_multiple_skills_are_merged_chronologically(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("XMC_V2_SKILLS_DIR", str(tmp_path))
        now = time.time()
        _seed(tmp_path, "alpha", [
            {"kind": "promote", "skill_id": "alpha", "from_version": 0, "to_version": 1,
             "ts": now - 7200, "evidence": ["mean=0.8"]},
        ])
        _seed(tmp_path, "beta", [
            {"kind": "promote", "skill_id": "beta", "from_version": 0, "to_version": 1,
             "ts": now - 3600, "evidence": ["mean=0.85"]},
        ])
        rc = run_evolution_show(None)
        assert rc == 0
        out = capsys.readouterr().out
        # alpha (older) should appear before beta in output
        assert out.index("alpha") < out.index("beta")


# ---------------------------------------------------------------------------
# Round-trip against real SkillRegistry._persist format
# ---------------------------------------------------------------------------
class TestRegistryRoundTrip:
    """If SkillRegistry.promote() writes a record, the CLI must read it.

    This test is the seam: if anyone ever renames a field in
    ``PromotionRecord``, this test breaks before the CLI silently goes
    blind. No mock — we use the real registry.
    """

    def test_cli_reads_what_registry_wrote(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("XMC_V2_SKILLS_DIR", str(tmp_path))

        class _NoopSkill:
            id = "round_trip"
            version = 1

        class _NoopManifest:
            id = "round_trip"
            version = 1

        registry = SkillRegistry(history_dir=tmp_path)
        registry.register(_NoopSkill(), _NoopManifest())  # v1

        class _V2(_NoopSkill):
            version = 2

        class _V2Manifest(_NoopManifest):
            version = 2

        registry.register(_V2(), _V2Manifest())

        registry.promote("round_trip", 2, evidence=["mean=0.911", "plays=12"])
        registry.rollback("round_trip", 1, reason="caller changed their mind")

        rc = run_evolution_show(None)
        assert rc == 0
        out = capsys.readouterr().out
        assert "round_trip" in out
        assert "0.911" in out
        assert "caller changed their mind" in out


# ---------------------------------------------------------------------------
# Typer integration
# ---------------------------------------------------------------------------
class TestTyperWiring:
    def test_evolution_show_is_a_registered_subcommand(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XMC_V2_SKILLS_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(app, ["evolution", "show"])
        assert result.exit_code == 0, result.output
        assert "No evolution events" in result.output

    def test_help_surfaces_evolution_group(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "evolution" in result.output
