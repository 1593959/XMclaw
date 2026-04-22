"""Unit tests for scripts/test_changed.py — the smart-gate lane selector (Epic #11).

The selector is a build-time script, not a runtime module, so we load it via
``importlib.util`` rather than teaching tests to mutate ``sys.path``. The
imported module is cached on the ``_loaded`` closure so every test sees the
same object.

What we cover:
  * ``_parse_yaml_lanes`` survives the real shipped ``scripts/test_lanes.yaml``
    *and* a hand-rolled fixture — confirms the handwritten parser understands
    the subset of YAML we actually use.
  * ``select_tests`` over the key contract paths: always-lane, targeted lane
    match, full-suite fallback via ``__all__``, pure-docs change → empty,
    direct test-file change without a lane match.
  * ``build_pytest_cmd`` returns ``None`` / ``pytest tests/`` / explicit files
    for the three shapes the CLI driver has to render.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "test_changed.py"
LANES_PATH = REPO_ROOT / "scripts" / "test_lanes.yaml"


def _load_module():
    # Register in sys.modules BEFORE exec so dataclass field resolution
    # (which looks up the owning module to resolve string annotations) can
    # find it — otherwise @dataclass raises AttributeError on PY 3.10.
    name = "_smart_gate_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec and spec.loader, "failed to spec test_changed.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tc():
    return _load_module()


# ── _parse_yaml_lanes ──────────────────────────────────────────────────


def test_parse_yaml_lanes_reads_shipped_file(tc) -> None:
    """The real lanes file must always parse — it's what CI uses."""
    lanes = tc.load_lanes(LANES_PATH)
    names = [lane.name for lane in lanes]
    # These are the anchors everyone else depends on. If they go missing,
    # CI's smart-gate would silently skip entire subsystems.
    for expected in ("always", "bus", "llm", "tools", "full_fallback"):
        assert expected in names, f"missing lane: {expected}"
    always = next(lane for lane in lanes if lane.name == "always")
    assert "__always__" in always.triggers
    fallback = next(lane for lane in lanes if lane.name == "full_fallback")
    assert "__all__" in fallback.tests


def test_parse_yaml_lanes_handles_inline_list(tc, tmp_path: Path) -> None:
    yaml = (
        "version: 1\n"
        "lanes:\n"
        "  alpha:\n"
        "    triggers: [\"src/a/**\", \"src/b/**\"]\n"
        "    tests: [\"tests/a.py\"]\n"
    )
    path = tmp_path / "lanes.yaml"
    path.write_text(yaml, encoding="utf-8")
    lanes = tc.load_lanes(path)
    assert len(lanes) == 1
    assert lanes[0].name == "alpha"
    assert lanes[0].triggers == ("src/a/**", "src/b/**")
    assert lanes[0].tests == ("tests/a.py",)


def test_parse_yaml_lanes_handles_block_list(tc, tmp_path: Path) -> None:
    yaml = (
        "lanes:\n"
        "  beta:\n"
        "    triggers:\n"
        "      - \"pkg/**\"\n"
        "      - \"other/**\"\n"
        "    tests:\n"
        "      - tests/x.py\n"
        "      - tests/y.py\n"
    )
    path = tmp_path / "lanes.yaml"
    path.write_text(yaml, encoding="utf-8")
    lanes = tc.load_lanes(path)
    assert lanes[0].triggers == ("pkg/**", "other/**")
    assert lanes[0].tests == ("tests/x.py", "tests/y.py")


def test_parse_yaml_lanes_ignores_comments(tc, tmp_path: Path) -> None:
    yaml = (
        "# top-level comment\n"
        "lanes:\n"
        "  gamma:  # inline\n"
        "    triggers: [\"a/**\"]\n"
        "    tests: [\"tests/g.py\"]\n"
    )
    path = tmp_path / "lanes.yaml"
    path.write_text(yaml, encoding="utf-8")
    lanes = tc.load_lanes(path)
    assert lanes[0].name == "gamma"
    assert lanes[0].tests == ("tests/g.py",)


def test_load_lanes_missing_file_raises(tc, tmp_path: Path) -> None:
    with pytest.raises(tc.ConfigError):
        tc.load_lanes(tmp_path / "does-not-exist.yaml")


def test_parse_yaml_lanes_empty_raises(tc, tmp_path: Path) -> None:
    path = tmp_path / "lanes.yaml"
    path.write_text("version: 1\n", encoding="utf-8")
    with pytest.raises(tc.ConfigError):
        tc.load_lanes(path)


# ── select_tests ────────────────────────────────────────────────────────


@pytest.fixture
def lanes(tc):
    return tc.load_lanes(LANES_PATH)


def test_select_tests_empty_diff_returns_nothing(tc, lanes) -> None:
    """No changes at all → the always lane should still not fire, because
    its trigger is ``__always__`` which checks that SOMETHING changed."""
    tests, matched = tc.select_tests([], lanes)
    assert tests == []
    assert matched == []


def test_select_tests_docs_only_returns_nothing(tc, lanes) -> None:
    """Pure-docs changes should not burn CI minutes. The always lane DOES
    fire (any change → run import surface + config shape tests) so the
    selector returns those two tests plus nothing else."""
    tests, matched = tc.select_tests(
        ["docs/ARCHITECTURE.md", "README.md"], lanes,
    )
    # Always lane fires on any diff, so we get its cheap sanity tests.
    assert "always" in matched
    assert any("import_surface" in t for t in tests)
    assert any("config_example" in t for t in tests)
    # But no subsystem lane should pick these up.
    for lane_name in ("bus", "llm", "tools", "daemon"):
        assert lane_name not in matched


def test_select_tests_security_change_picks_security_lane(tc, lanes) -> None:
    changed = ["xmclaw/security/prompt_scanner.py"]
    tests, matched = tc.select_tests(changed, lanes)
    assert "security" in matched
    assert "tests/unit/test_v2_prompt_scanner.py" in tests
    assert "tests/integration/test_v2_prompt_injection.py" in tests


def test_select_tests_pyproject_triggers_full_fallback(tc, lanes) -> None:
    tests, matched = tc.select_tests(["pyproject.toml"], lanes)
    assert tests == ["__all__"]
    assert "full_fallback" in matched


def test_select_tests_lockfile_triggers_full_fallback(tc, lanes) -> None:
    tests, matched = tc.select_tests(["requirements-lock.txt"], lanes)
    assert tests == ["__all__"]


def test_select_tests_ci_workflow_triggers_full_fallback(tc, lanes) -> None:
    tests, matched = tc.select_tests(
        [".github/workflows/python-package-conda.yml"], lanes,
    )
    assert tests == ["__all__"]


def test_select_tests_direct_test_change_runs_that_file(tc, lanes) -> None:
    """A change to a test file with no matching source lane should still
    run just that test — without it, editing a test wouldn't execute it."""
    changed = ["tests/unit/test_v2_prompt_scanner.py"]
    tests, matched = tc.select_tests(changed, lanes)
    # The always lane still fires (any change); plus our direct test.
    assert "tests/unit/test_v2_prompt_scanner.py" in tests
    assert "direct_tests" in matched


def test_select_tests_direct_test_ignores_init(tc, lanes) -> None:
    """tests/__init__.py is not runnable and shouldn't be fed to pytest."""
    tests, _ = tc.select_tests(["tests/unit/__init__.py"], lanes)
    assert "tests/unit/__init__.py" not in tests


def test_select_tests_additive_union_across_lanes(tc, lanes) -> None:
    """Two lanes matching → union of their tests, de-duplicated."""
    changed = [
        "xmclaw/core/bus/events.py",          # bus lane
        "xmclaw/providers/llm/openai.py",     # llm lane
    ]
    tests, matched = tc.select_tests(changed, lanes)
    assert "bus" in matched
    assert "llm" in matched
    assert "tests/unit/test_v2_bus_ping.py" in tests
    assert "tests/unit/test_v2_openai_provider.py" in tests
    # De-dup sanity: sorted unique list.
    assert tests == sorted(set(tests))


def test_select_tests_full_all_short_circuits(tc) -> None:
    """When a full-fallback lane matches, we should NOT also enumerate
    individual tests — ``["__all__"]`` is the contract."""
    synthetic = (
        tc.Lane("s", ("src/**",), ("tests/one.py",)),
        tc.Lane("f", ("pyproject.toml",), ("__all__",)),
    )
    tests, matched = tc.select_tests(
        ["src/app.py", "pyproject.toml"], synthetic,
    )
    assert tests == ["__all__"]
    assert "s" in matched and "f" in matched


def test_select_tests_always_trigger_requires_any_change(tc) -> None:
    synthetic = (
        tc.Lane("always", ("__always__",), ("tests/cheap.py",)),
    )
    # No changes → no always-fire.
    assert tc.select_tests([], synthetic) == ([], [])
    # Any change → always fires.
    tests, matched = tc.select_tests(["README.md"], synthetic)
    assert tests == ["tests/cheap.py"]
    assert matched == ["always"]


# ── build_pytest_cmd ────────────────────────────────────────────────────


def test_build_pytest_cmd_none_for_empty(tc) -> None:
    assert tc.build_pytest_cmd([]) is None


def test_build_pytest_cmd_full_suite_sentinel(tc) -> None:
    cmd = tc.build_pytest_cmd(["__all__"])
    assert cmd is not None
    assert cmd[-3:] == ["-m", "pytest", "tests/"]


def test_build_pytest_cmd_explicit_paths(tc) -> None:
    cmd = tc.build_pytest_cmd(["tests/a.py", "tests/b.py"])
    assert cmd is not None
    assert cmd[-2:] == ["tests/a.py", "tests/b.py"]


def test_build_pytest_cmd_forwards_extra_args(tc) -> None:
    cmd = tc.build_pytest_cmd(["tests/a.py"], extra=["-v", "-k", "foo"])
    assert cmd is not None
    assert cmd[-3:] == ["-v", "-k", "foo"]
    # The test file comes before the extra args.
    assert "tests/a.py" in cmd
    assert cmd.index("tests/a.py") < cmd.index("-v")
