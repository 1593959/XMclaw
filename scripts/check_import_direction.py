"""CI-1 (V2_DEVELOPMENT.md §6.3): enforce xmclaw v2 module layering.

Two layering rules are enforced:

1. ``xmclaw/core/**`` may not import from ``xmclaw.providers.*`` or
   ``xmclaw.skills.*``. The causal axis is core -> (providers, skills),
   never the reverse. Providers and skills are downstream of core.

2. ``xmclaw/utils/**`` may not import from any other ``xmclaw.*``
   subpackage (only ``xmclaw.utils.*`` self-imports are allowed). Utils
   is the leaf of the dependency DAG — every higher-level subpackage
   may depend on utils, so utils may not depend on anything higher.
   This is what `xmclaw/utils/AGENTS.md` §4 has been flagging as TODO.

Exit 0 if clean, 1 if any violation found. Prints each violation so CI
surfaces the file and line.

Usage:
  python scripts/check_import_direction.py
"""
from __future__ import annotations

import ast
import pathlib
import sys
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).parent.parent


@dataclass(frozen=True)
class Rule:
    """One layering rule: a source subtree and the prefixes it may not
    import from. ``allowed_self_prefix`` lets a rule carve out a self-
    import exception (utils imports of other utils modules are fine)."""

    name: str
    source_dir: pathlib.Path
    forbidden_prefixes: tuple[str, ...]
    allowed_self_prefix: str | None = None

    def applies_to(self, module: str) -> bool:
        if not any(module.startswith(p) for p in self.forbidden_prefixes):
            return False
        if self.allowed_self_prefix and module.startswith(self.allowed_self_prefix):
            return False
        return True


RULES: tuple[Rule, ...] = (
    Rule(
        name="core cannot import from providers or skills",
        source_dir=ROOT / "xmclaw" / "core",
        forbidden_prefixes=("xmclaw.providers", "xmclaw.skills"),
    ),
    Rule(
        name="utils cannot import from other xmclaw subpackages",
        source_dir=ROOT / "xmclaw" / "utils",
        forbidden_prefixes=("xmclaw.",),
        allowed_self_prefix="xmclaw.utils",
    ),
)


@dataclass
class Violation:
    rule: Rule
    file: pathlib.Path
    line: int
    statement: str


def scan_file(path: pathlib.Path, rule: Rule) -> list[Violation]:
    violations: list[Violation] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        print(f"WARN: could not parse {path}: {e}", file=sys.stderr)
        return violations

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if rule.applies_to(alias.name):
                    violations.append(
                        Violation(rule, path, node.lineno, f"import {alias.name}")
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if rule.applies_to(mod):
                names = ", ".join(a.name for a in node.names)
                violations.append(
                    Violation(rule, path, node.lineno, f"from {mod} import {names}")
                )
    return violations


def scan_rule(rule: Rule) -> list[Violation]:
    if not rule.source_dir.exists():
        print(f"WARN: {rule.source_dir} does not exist; skipping rule", file=sys.stderr)
        return []
    violations: list[Violation] = []
    for py_file in rule.source_dir.rglob("*.py"):
        violations.extend(scan_file(py_file, rule))
    return violations


def main() -> int:
    all_violations: list[Violation] = []
    for rule in RULES:
        all_violations.extend(scan_rule(rule))

    if not all_violations:
        print(f"OK: {len(RULES)} layering rule(s) clean")
        return 0

    # Group by rule for readable output.
    by_rule: dict[str, list[Violation]] = {}
    for v in all_violations:
        by_rule.setdefault(v.rule.name, []).append(v)

    print(f"FAIL: {len(all_violations)} import-direction violation(s):")
    for rule_name, vs in by_rule.items():
        print(f"\n  [{rule_name}] — {len(vs)} violation(s):")
        for v in vs:
            rel = v.file.relative_to(ROOT)
            print(f"    {rel}:{v.line}  {v.statement}")
    print(
        "\nSee docs/V2_DEVELOPMENT.md §2 and xmclaw/<subdir>/AGENTS.md for "
        "the full layering contract."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
