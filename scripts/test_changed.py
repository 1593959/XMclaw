"""Select pytest targets based on git diff (Epic #11).

Reads ``scripts/test_lanes.yaml`` and a list of changed paths (from
``git diff`` by default) and emits a pytest invocation covering just
the affected lanes.

Usage
-----

    python scripts/test_changed.py                 # vs. working tree
    python scripts/test_changed.py --base origin/main    # vs. a ref
    python scripts/test_changed.py --from-stdin    # read paths from stdin
    python scripts/test_changed.py --all           # force full suite
    python scripts/test_changed.py --dry-run       # print command, don't run

Exit codes
----------
* ``0`` — pytest succeeded, or no tests selected (pure-docs change).
* ``1`` — pytest failed.
* ``2`` — argument / config error.

The selector is deliberately conservative: duplicates get de-duped, but
transitive deps are NOT followed. If your change at
``xmclaw/core/bus/events.py`` breaks an LLM translator, add the right
lane trigger rather than making the selector smarter — explicit beats
clever for a test gate.
"""
from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LANES_FILE = REPO_ROOT / "scripts" / "test_lanes.yaml"


@dataclass(frozen=True, slots=True)
class Lane:
    name: str
    triggers: tuple[str, ...]
    tests: tuple[str, ...]


class ConfigError(ValueError):
    pass


def _parse_yaml_lanes(text: str) -> tuple[Lane, ...]:
    """Minimal YAML reader — only enough to parse test_lanes.yaml.

    We parse by hand so the script has no third-party deps and runs in a
    bare Python install (what CI uses before `pip install -e ".[dev]"`).
    Format assumptions baked in:
      * 2-space indentation
      * No anchors / aliases / flow style
      * Values are strings or lists of strings
    """
    lanes: list[Lane] = []
    current_name: str | None = None
    current_triggers: list[str] = []
    current_tests: list[str] = []
    current_key: str | None = None

    def _flush() -> None:
        nonlocal current_name, current_triggers, current_tests, current_key
        if current_name is not None:
            lanes.append(Lane(
                name=current_name,
                triggers=tuple(current_triggers),
                tests=tuple(current_tests),
            ))
        current_name = None
        current_triggers = []
        current_tests = []
        current_key = None

    in_lanes = False
    for raw_line in text.splitlines():
        # Strip comments (anything from a '#' outside of a string). Our
        # lane file uses only simple strings, so a naive split is enough.
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.rstrip(":") == "lanes":
            in_lanes = True
            continue
        if not in_lanes:
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 2 and stripped.endswith(":"):
            _flush()
            current_name = stripped[:-1].strip()
            continue
        if indent == 4 and stripped.endswith(":"):
            current_key = stripped[:-1].strip()
            if current_key == "triggers":
                current_triggers = []
            elif current_key == "tests":
                current_tests = []
            continue
        if indent == 4 and ":" in stripped:
            # inline list form: `triggers: ["a", "b"]`
            k, _, v = stripped.partition(":")
            k = k.strip()
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                items = [
                    item.strip().strip('"').strip("'")
                    for item in v[1:-1].split(",")
                    if item.strip()
                ]
                if k == "triggers":
                    current_triggers = items
                elif k == "tests":
                    current_tests = items
                continue
        if indent >= 6 and stripped.startswith("- "):
            item = stripped[2:].strip().strip('"').strip("'")
            if current_key == "triggers":
                current_triggers.append(item)
            elif current_key == "tests":
                current_tests.append(item)
            continue
    _flush()
    if not lanes:
        raise ConfigError("no lanes parsed — is test_lanes.yaml well-formed?")
    return tuple(lanes)


def load_lanes(path: Path = DEFAULT_LANES_FILE) -> tuple[Lane, ...]:
    if not path.exists():
        raise ConfigError(f"lanes file not found: {path}")
    return _parse_yaml_lanes(path.read_text(encoding="utf-8"))


def _changed_from_git(base: str | None) -> list[str]:
    """Return repo-relative paths that differ from ``base``.

    ``base=None`` means "uncommitted + staged working tree changes" — the
    mode pre-commit hooks use. ``base="origin/main"`` or ``base="main"``
    means "three-dot diff from the merge base" — what CI uses.
    """
    if base is None:
        # Working-tree changes (staged + unstaged) vs. HEAD.
        cmd = ["git", "diff", "--name-only", "HEAD"]
    else:
        cmd = ["git", "diff", "--name-only", f"{base}...HEAD"]
    try:
        out = subprocess.run(  # noqa: S603
            cmd, cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise ConfigError(
            f"git diff failed: {exc.stderr.strip() or exc}"
        ) from exc
    except FileNotFoundError as exc:
        raise ConfigError("git executable not found on PATH") from exc
    return [p for p in out.stdout.splitlines() if p]


def select_tests(
    changed: Sequence[str], lanes: Sequence[Lane],
) -> tuple[list[str], list[str]]:
    """Return (tests_to_run, matched_lane_names).

    ``tests_to_run`` is the de-duplicated, sorted list of test file
    paths (or the literal ``"__all__"`` sentinel if any lane requires
    the full suite). Callers render it into a pytest command.

    Special rules:
      * A changed path that IS itself a test file always runs directly
        (even if no lane matches its source path).
      * The ``always`` lane runs if any source/test change exists at all.
      * ``full_fallback`` (or any lane whose ``tests`` includes
        ``__all__``) short-circuits everything and returns ``["__all__"]``.
    """
    matched: list[str] = []
    selected: set[str] = set()
    want_all = False

    changed_list = list(changed)

    # Direct-changed tests always run.
    direct_tests = [
        p for p in changed_list
        if p.startswith("tests/") and p.endswith(".py") and "__init__" not in p
    ]

    for lane in lanes:
        hit = False
        for trig in lane.triggers:
            if trig == "__always__":
                if changed_list:
                    hit = True
                break
            for p in changed_list:
                if fnmatch.fnmatch(p, trig):
                    hit = True
                    break
            if hit:
                break
        if hit:
            matched.append(lane.name)
            for t in lane.tests:
                if t == "__all__":
                    want_all = True
                else:
                    selected.add(t)

    if want_all:
        return (["__all__"], matched)

    # Direct-test changes always run regardless of lane matching.
    for t in direct_tests:
        selected.add(t)
        if "direct_tests" not in matched:
            matched.append("direct_tests")

    return (sorted(selected), matched)


def build_pytest_cmd(
    tests: Sequence[str], extra: Sequence[str] = (),
) -> list[str] | None:
    """Render the final ``python -m pytest`` invocation, or None if
    nothing is selected (pure-docs change). ``extra`` is forwarded after
    the test paths so callers can pass ``-v``, ``-k foo``, etc."""
    if not tests:
        return None
    if len(tests) == 1 and tests[0] == "__all__":
        return [sys.executable, "-m", "pytest", "tests/", *extra]
    return [sys.executable, "-m", "pytest", *tests, *extra]


def _format_plan(
    changed: Sequence[str], matched: Sequence[str],
    tests: Sequence[str], cmd: Sequence[str] | None,
) -> str:
    lines = ["smart-gate plan:"]
    lines.append(f"  changed paths: {len(changed)}")
    for p in changed[:15]:
        lines.append(f"    - {p}")
    if len(changed) > 15:
        lines.append(f"    ... ({len(changed) - 15} more)")
    lines.append(f"  matched lanes: {', '.join(matched) or '(none)'}")
    if not tests:
        lines.append("  selected tests: (none — skipping pytest)")
    elif tests == ["__all__"]:
        lines.append("  selected tests: full suite (tests/)")
    else:
        lines.append(f"  selected tests: {len(tests)} file(s)")
        for t in tests:
            lines.append(f"    - {t}")
    if cmd is None:
        lines.append("  pytest command: (skipped — nothing to run)")
    else:
        lines.append("  pytest command: " + " ".join(cmd))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the minimal pytest subset for the current diff.",
    )
    parser.add_argument(
        "--base", default=None,
        help="Git ref to diff against. Default: working-tree diff vs HEAD.",
    )
    parser.add_argument(
        "--from-stdin", action="store_true",
        help="Read newline-separated paths from stdin instead of git diff.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Skip diff selection and run the full suite.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan and pytest command but don't execute pytest.",
    )
    parser.add_argument(
        "--lanes-file", default=str(DEFAULT_LANES_FILE),
        help="Override the lanes YAML path.",
    )
    parser.add_argument(
        "pytest_args", nargs="*",
        help="Extra args passed through to pytest (use `--` before them).",
    )
    args = parser.parse_args(argv)

    try:
        lanes = load_lanes(Path(args.lanes_file))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.all:
        changed = ["__all__"]
        matched = ["forced_full"]
        tests = ["__all__"]
    else:
        if args.from_stdin:
            changed = [p.strip() for p in sys.stdin.read().splitlines() if p.strip()]
        else:
            try:
                changed = _changed_from_git(args.base)
            except ConfigError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        tests, matched = select_tests(changed, lanes)

    cmd = build_pytest_cmd(tests, args.pytest_args)
    print(_format_plan(changed, matched, tests, cmd))
    if args.dry_run or cmd is None:
        return 0
    result = subprocess.run(cmd, cwd=REPO_ROOT)  # noqa: S603
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
