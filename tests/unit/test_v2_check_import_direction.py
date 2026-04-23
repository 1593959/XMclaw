"""Unit tests for ``scripts/check_import_direction.py``.

Two rules are enforced: core-cannot-import-providers-or-skills, and
utils-cannot-import-any-other-xmclaw-subpackage. These tests pin both
directions, the self-import carve-out, and the regression guard that
the shipped tree stays clean.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check_import_direction.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_import_direction", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["check_import_direction"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def checker():
    return _load_module()


def _write(tmp_path: Path, rel: str, body: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


# ── Rule.applies_to behavior ─────────────────────────────────────────

def test_core_rule_flags_providers_import(checker):
    rule = checker.RULES[0]
    assert "core cannot import" in rule.name
    assert rule.applies_to("xmclaw.providers.tool.builtin")
    assert rule.applies_to("xmclaw.skills.registry")


def test_core_rule_allows_unrelated_imports(checker):
    rule = checker.RULES[0]
    assert not rule.applies_to("xmclaw.utils.paths")
    assert not rule.applies_to("xmclaw.core.bus")
    assert not rule.applies_to("pathlib")


def test_utils_rule_flags_any_other_xmclaw_subpackage(checker):
    rule = checker.RULES[1]
    assert "utils cannot import" in rule.name
    assert rule.applies_to("xmclaw.core.bus")
    assert rule.applies_to("xmclaw.daemon.factory")
    assert rule.applies_to("xmclaw.providers.llm.anthropic")
    assert rule.applies_to("xmclaw.security.policy")
    assert rule.applies_to("xmclaw.cli.main")


def test_utils_rule_allows_self_imports(checker):
    rule = checker.RULES[1]
    # The whole point of the carve-out.
    assert not rule.applies_to("xmclaw.utils.paths")
    assert not rule.applies_to("xmclaw.utils.redact")


def test_utils_rule_allows_stdlib_imports(checker):
    rule = checker.RULES[1]
    assert not rule.applies_to("os")
    assert not rule.applies_to("pathlib")
    assert not rule.applies_to("structlog")


# ── scan_file integration ───────────────────────────────────────────

def test_scan_file_detects_import_from(checker, tmp_path):
    bad = _write(
        tmp_path, "xmclaw/utils/bad.py",
        "from xmclaw.core.bus import EventType\n",
    )
    rule = checker.Rule(
        name="utils test rule",
        source_dir=tmp_path / "xmclaw" / "utils",
        forbidden_prefixes=("xmclaw.",),
        allowed_self_prefix="xmclaw.utils",
    )
    violations = checker.scan_file(bad, rule)
    assert len(violations) == 1
    assert violations[0].line == 1
    assert "from xmclaw.core.bus import" in violations[0].statement


def test_scan_file_detects_plain_import(checker, tmp_path):
    bad = _write(
        tmp_path, "xmclaw/utils/bad.py",
        "import xmclaw.core.bus\n",
    )
    rule = checker.Rule(
        name="utils test rule",
        source_dir=tmp_path / "xmclaw" / "utils",
        forbidden_prefixes=("xmclaw.",),
        allowed_self_prefix="xmclaw.utils",
    )
    violations = checker.scan_file(bad, rule)
    assert len(violations) == 1
    assert "import xmclaw.core.bus" in violations[0].statement


def test_scan_file_honors_self_import_exception(checker, tmp_path):
    good = _write(
        tmp_path, "xmclaw/utils/good.py",
        "from xmclaw.utils.paths import data_dir\n"
        "from xmclaw.utils.redact import redact_string\n",
    )
    rule = checker.Rule(
        name="utils test rule",
        source_dir=tmp_path / "xmclaw" / "utils",
        forbidden_prefixes=("xmclaw.",),
        allowed_self_prefix="xmclaw.utils",
    )
    assert checker.scan_file(good, rule) == []


def test_scan_file_tolerates_syntax_error(checker, tmp_path, capsys):
    # A syntax-broken file produces a warning on stderr but doesn't crash
    # the whole pass — one file out of many shouldn't stop the scan.
    bad = _write(tmp_path, "xmclaw/utils/broken.py", "def foo(:\n")
    rule = checker.Rule(
        name="utils test rule",
        source_dir=tmp_path / "xmclaw" / "utils",
        forbidden_prefixes=("xmclaw.",),
        allowed_self_prefix="xmclaw.utils",
    )
    assert checker.scan_file(bad, rule) == []
    assert "could not parse" in capsys.readouterr().err


# ── regression guard on the shipped tree ─────────────────────────────

def test_shipped_tree_is_clean(checker):
    """The whole point of the extension — the tree we ship must stay
    layered correctly. This is the guard that turns the check into
    something CI-enforceable."""
    violations: list = []
    for rule in checker.RULES:
        violations.extend(checker.scan_rule(rule))
    assert violations == [], "\n".join(
        f"{v.file.relative_to(_ROOT)}:{v.line}  [{v.rule.name}]  {v.statement}"
        for v in violations
    )
