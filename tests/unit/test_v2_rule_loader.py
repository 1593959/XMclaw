"""Coverage for ``xmclaw/security/rule_loader.py`` — audit B-P0-7.

The loader and scanner are exercised indirectly through the
``RuleBasedToolGuardian`` happy path, which leaves all the resilience
branches uncovered: bad YAML, malformed entries, broken regex,
exclude-pattern matches, dedup across multiple patterns, ``file_types:
[binary]`` skip, default-fallback severity. This file pins them down so
a future YAML-format tweak in ``xmclaw/security/rules/*.yaml`` can't
silently make the loader drop rules.

We construct rule files in a tmp directory and aim ``load_rules`` at
it, rather than mutating the bundled rule set.
"""
from __future__ import annotations

from pathlib import Path


from xmclaw.security.rule_loader import (
    Finding,
    Rule,
    Severity,
    load_rules,
    scan_with_rules,
)


# ── load_rules: directory contracts ────────────────────────────────────


def test_load_rules_returns_empty_when_directory_missing(tmp_path: Path) -> None:
    """A non-existent directory must return ``[]`` rather than raise —
    the daemon needs to boot even when the optional rules dir is gone."""
    nowhere = tmp_path / "does-not-exist"
    assert not nowhere.exists()
    assert load_rules(nowhere) == []


def test_load_rules_empty_directory_returns_empty(tmp_path: Path) -> None:
    """A directory with no YAML files is also legitimate (e.g. fresh
    install before bundled rules ship)."""
    assert load_rules(tmp_path) == []


def test_load_rules_default_dir_finds_bundled_rules() -> None:
    """The no-arg path must reach the packaged ``rules/`` directory.
    We only assert non-empty so adding/removing rule files in the
    repo doesn't churn this test."""
    rules = load_rules()
    assert len(rules) > 0
    # All entries are well-formed Rule objects — sanity check after the
    # YAML round-trip.
    for r in rules:
        assert isinstance(r, Rule)
        assert r.id
        assert r.patterns


# ── load_rules: per-file resilience ────────────────────────────────────


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_load_rules_skips_files_with_unparseable_yaml(tmp_path: Path) -> None:
    _write(tmp_path / "broken.yaml", ":\n  this is: : : not yaml")
    _write(tmp_path / "good.yaml", """
- id: GOOD_RULE
  category: test
  severity: low
  patterns: ["hello"]
""")
    rules = load_rules(tmp_path)
    ids = {r.id for r in rules}
    assert ids == {"GOOD_RULE"}, "broken yaml must be skipped, not propagate"


def test_load_rules_skips_top_level_non_list(tmp_path: Path) -> None:
    """A YAML file whose top level is a dict (or scalar) is not a
    rule list — must be skipped, not crash the loader."""
    _write(tmp_path / "wrong_shape.yaml", "id: not_a_list\nseverity: low\n")
    _write(tmp_path / "good.yaml", """
- id: ONLY_GOOD
  patterns: ["x"]
""")
    rules = load_rules(tmp_path)
    assert {r.id for r in rules} == {"ONLY_GOOD"}


def test_load_rules_skips_non_dict_entries(tmp_path: Path) -> None:
    _write(tmp_path / "mixed.yaml", """
- "a string entry"
- 42
- id: REAL_ONE
  patterns: ["match"]
""")
    rules = load_rules(tmp_path)
    assert {r.id for r in rules} == {"REAL_ONE"}


def test_load_rules_skips_entries_missing_id(tmp_path: Path) -> None:
    _write(tmp_path / "missing_id.yaml", """
- patterns: ["foo"]
- id: HAS_ID
  patterns: ["bar"]
""")
    rules = load_rules(tmp_path)
    assert {r.id for r in rules} == {"HAS_ID"}


def test_load_rules_skips_entries_with_no_compilable_patterns(
    tmp_path: Path,
) -> None:
    """``patterns: []`` and ``patterns: ["[bad"]`` (which fails to
    compile) must both produce zero compiled patterns and be dropped."""
    _write(tmp_path / "no_patterns.yaml", """
- id: EMPTY_PATTERNS
  patterns: []
- id: BROKEN_REGEX
  patterns: ["[unclosed"]
- id: GOOD
  patterns: ["ok"]
""")
    rules = load_rules(tmp_path)
    assert {r.id for r in rules} == {"GOOD"}


def test_load_rules_recovers_per_pattern_when_some_regex_broken(
    tmp_path: Path,
) -> None:
    """One bad regex in a list of three must not poison the whole rule —
    the rule survives with the two compilable patterns."""
    _write(tmp_path / "partial.yaml", """
- id: PARTIAL
  patterns:
    - "[unclosed"
    - "good_pattern_a"
    - "good_pattern_b"
""")
    rules = load_rules(tmp_path)
    assert len(rules) == 1
    rule = rules[0]
    assert rule.id == "PARTIAL"
    # 2 of 3 patterns compiled.
    assert len(rule.patterns) == 2


def test_load_rules_unknown_severity_falls_back_to_medium(
    tmp_path: Path,
) -> None:
    """``_parse_severity`` swallows ``ValueError`` and returns MEDIUM —
    cheap defense against a typo in someone's YAML."""
    _write(tmp_path / "bad_sev.yaml", """
- id: BAD_SEVERITY
  severity: catastrophic
  patterns: ["x"]
""")
    rules = load_rules(tmp_path)
    assert len(rules) == 1
    assert rules[0].severity == Severity.MEDIUM


def test_load_rules_uppercase_severity_works(tmp_path: Path) -> None:
    _write(tmp_path / "case.yaml", """
- id: UPPER_SEV
  severity: HIGH
  patterns: ["x"]
""")
    rules = load_rules(tmp_path)
    assert rules[0].severity == Severity.HIGH


def test_load_rules_propagates_exclude_patterns(tmp_path: Path) -> None:
    _write(tmp_path / "with_exclude.yaml", """
- id: WITH_EXCLUDE
  patterns: ["secret"]
  exclude_patterns: ["my-test-secret"]
""")
    rules = load_rules(tmp_path)
    assert len(rules[0].exclude_patterns) == 1


# ── scan_with_rules: matching, dedup, excludes, binary skip ────────────


def test_scan_returns_finding_with_full_metadata(tmp_path: Path) -> None:
    _write(tmp_path / "scan.yaml", """
- id: TEST_HIT
  category: test_cat
  severity: high
  patterns: ["forbidden"]
  description: "found a forbidden token"
  remediation: "remove it"
""")
    rules = load_rules(tmp_path)
    findings = scan_with_rules("hello forbidden world", rules=rules)
    assert len(findings) == 1
    f = findings[0]
    assert isinstance(f, Finding)
    assert f.rule_id == "TEST_HIT"
    assert f.category == "test_cat"
    assert f.severity == Severity.HIGH
    assert f.matched_text == "forbidden"
    assert f.description == "found a forbidden token"
    assert f.remediation == "remove it"


def test_scan_dedups_per_rule_when_multiple_patterns_match(
    tmp_path: Path,
) -> None:
    """Two patterns of the same rule both match → only ONE Finding."""
    _write(tmp_path / "dedup.yaml", """
- id: DUP
  patterns:
    - "alpha"
    - "beta"
""")
    rules = load_rules(tmp_path)
    findings = scan_with_rules("alpha and beta both appear", rules=rules)
    rule_ids = [f.rule_id for f in findings]
    assert rule_ids.count("DUP") == 1


def test_scan_skips_when_exclude_pattern_matches(tmp_path: Path) -> None:
    """A real-world false-positive guard: AWS_ACCESS_KEY-style regex hits
    a string that the exclude pattern marks as a known fixture."""
    _write(tmp_path / "fp.yaml", """
- id: AWS_ACCESS_KEY_GUARDED
  patterns: ['AKIA[0-9A-Z]{16}']
  exclude_patterns: ['AKIAIOSFODNN7EXAMPLE']
""")
    rules = load_rules(tmp_path)
    # Real-looking key — should fire.
    findings_real = scan_with_rules("token=AKIAABCDEFGHIJKLMNOP", rules=rules)
    assert any(f.rule_id == "AWS_ACCESS_KEY_GUARDED" for f in findings_real)
    # AWS docs example key — exclude_patterns suppresses it.
    findings_doc = scan_with_rules("token=AKIAIOSFODNN7EXAMPLE", rules=rules)
    assert findings_doc == []


def test_scan_skips_binary_only_rule(tmp_path: Path) -> None:
    """``file_types: [binary]`` rules don't apply when scanning plain
    text — verifies the ``rule.file_types == ['binary']`` guard."""
    _write(tmp_path / "binary_only.yaml", """
- id: BINARY_RULE
  patterns: ["MAGIC"]
  file_types: ["binary"]
- id: TEXT_RULE
  patterns: ["MAGIC"]
  file_types: ["text"]
""")
    rules = load_rules(tmp_path)
    findings = scan_with_rules("the MAGIC word is here", rules=rules)
    ids = {f.rule_id for f in findings}
    assert ids == {"TEXT_RULE"}


def test_scan_dedups_across_duplicate_rule_ids(tmp_path: Path) -> None:
    """If two ``Rule`` objects share an id, the second one is skipped
    after the first match — closes the cross-rule dedup branch."""
    _write(tmp_path / "dup_id.yaml", """
- id: SAME_ID
  patterns: ["alpha"]
- id: SAME_ID
  patterns: ["beta"]
""")
    rules = load_rules(tmp_path)
    assert len(rules) == 2  # both Rule objects exist
    findings = scan_with_rules("alpha and beta", rules=rules)
    assert [f.rule_id for f in findings] == ["SAME_ID"]


def test_scan_with_rules_loads_default_when_none_passed() -> None:
    """``rules=None`` triggers ``load_rules()`` — covers the default
    branch the rule_based guardian relies on for first-call boot."""
    findings = scan_with_rules(
        "API key: sk-ant-api03-abc"  # likely a hit; bundled rules look for keys
    )
    # We don't care which rule fired — just that the default-load path
    # was reachable. If the bundled rules ever stop having a single
    # secret-detector that triggers on this string, the worst case is
    # an empty list, which still proves the call didn't crash.
    assert isinstance(findings, list)
