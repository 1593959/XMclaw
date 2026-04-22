"""CI-1 (V2_DEVELOPMENT.md §6.3): enforce xmclaw v2 module layering.

Rule: ``xmclaw/core/**`` may not import from ``xmclaw.providers.*`` or
``xmclaw.skills.*``. The causal axis is core → (providers, skills),
never the reverse. Providers and skills are downstream of core.

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
CORE_DIR = ROOT / "xmclaw" / "core"

FORBIDDEN_PREFIXES = ("xmclaw.providers", "xmclaw.skills")


@dataclass
class Violation:
    file: pathlib.Path
    line: int
    statement: str


def scan_file(path: pathlib.Path) -> list[Violation]:
    violations: list[Violation] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        print(f"WARN: could not parse {path}: {e}", file=sys.stderr)
        return violations

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.startswith(p) for p in FORBIDDEN_PREFIXES):
                    violations.append(Violation(path, node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if any(mod.startswith(p) for p in FORBIDDEN_PREFIXES):
                names = ", ".join(a.name for a in node.names)
                violations.append(
                    Violation(path, node.lineno, f"from {mod} import {names}")
                )
    return violations


def main() -> int:
    if not CORE_DIR.exists():
        print(f"FATAL: {CORE_DIR} does not exist", file=sys.stderr)
        return 2

    all_violations: list[Violation] = []
    for py_file in CORE_DIR.rglob("*.py"):
        all_violations.extend(scan_file(py_file))

    if not all_violations:
        print(f"OK: {CORE_DIR.relative_to(ROOT)} has no forbidden imports")
        return 0

    print(f"FAIL: {len(all_violations)} import-direction violation(s):")
    for v in all_violations:
        rel = v.file.relative_to(ROOT)
        print(f"  {rel}:{v.line}  {v.statement}")
    print(
        "\nRule: xmclaw/core/** may not import from xmclaw.providers or "
        "xmclaw.skills. See docs/V2_DEVELOPMENT.md §2."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
