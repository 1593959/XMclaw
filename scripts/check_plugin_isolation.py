"""CI guard (Epic #2): third-party plugin code may only import
``xmclaw.plugin_sdk``.

Plugins sit *outside* the import DAG enforced by
``check_import_direction.py``. They are supposed to be a stable
boundary: when we refactor ``xmclaw.core`` or ``xmclaw.providers``,
plugins should keep working because they only saw the re-exports in
``xmclaw.plugin_sdk``.

This script walks ``xmclaw/plugins/**/*.py`` and fails on any
``import xmclaw.X`` / ``from xmclaw.X import ...`` where X is not
``plugin_sdk``. ``xmclaw.plugins.*`` self-imports are allowed — the
loader module under this tree is part of the plugin machinery, not a
plugin itself, and is exempted by name.

Exit 0 if clean, 1 if any violation found. Each violation prints its
file, line, and the offending statement so CI surfaces what to fix.

Usage:
    python scripts/check_plugin_isolation.py

Matches the pattern set by :mod:`scripts.check_import_direction`.
"""
from __future__ import annotations

import ast
import pathlib
import sys
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).parent.parent

# Files under xmclaw/plugins/ that are part of the plugin *machinery*
# (discovery, registration) rather than example plugins themselves.
# These are allowed to import from the rest of xmclaw/.
MACHINERY_EXEMPT: frozenset[str] = frozenset({
    "loader.py",     # entry_points discovery — needs importlib.metadata
    "__init__.py",   # package marker
})


@dataclass(frozen=True)
class Violation:
    file: pathlib.Path
    line: int
    statement: str


def _is_forbidden(module: str) -> bool:
    """True if ``module`` is an xmclaw import a plugin should not make."""
    if not module.startswith("xmclaw."):
        return False
    # xmclaw.plugin_sdk is the whole point — always allowed.
    if module == "xmclaw.plugin_sdk" or module.startswith("xmclaw.plugin_sdk."):
        return False
    # Plugins referring to their own sibling modules is fine — the loader
    # may register helpers next to a plugin entry point.
    if module == "xmclaw.plugins" or module.startswith("xmclaw.plugins."):
        return False
    return True


def scan_file(path: pathlib.Path) -> list[Violation]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        print(f"WARN: could not parse {path}: {exc}", file=sys.stderr)
        return []
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden(alias.name):
                    violations.append(
                        Violation(path, node.lineno, f"import {alias.name}")
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if _is_forbidden(mod):
                names = ", ".join(a.name for a in node.names)
                violations.append(
                    Violation(path, node.lineno, f"from {mod} import {names}")
                )
    return violations


def scan_plugins_tree(plugins_dir: pathlib.Path) -> list[Violation]:
    if not plugins_dir.exists():
        return []
    violations: list[Violation] = []
    for py_file in plugins_dir.rglob("*.py"):
        if py_file.name in MACHINERY_EXEMPT:
            continue
        violations.extend(scan_file(py_file))
    return violations


def main() -> int:
    plugins_dir = ROOT / "xmclaw" / "plugins"
    violations = scan_plugins_tree(plugins_dir)

    if not violations:
        scanned = sum(
            1 for p in plugins_dir.rglob("*.py") if p.name not in MACHINERY_EXEMPT
        ) if plugins_dir.exists() else 0
        print(f"OK: plugin isolation clean ({scanned} plugin file(s) scanned)")
        return 0

    print(
        f"FAIL: {len(violations)} plugin-isolation violation(s) — "
        f"plugins may only import xmclaw.plugin_sdk:"
    )
    for v in violations:
        rel = v.file.relative_to(ROOT)
        print(f"  {rel}:{v.line}  {v.statement}")
    print(
        "\nIf you need a symbol that isn't in xmclaw.plugin_sdk yet, "
        "open an issue — adding to the SDK is a versioned change. "
        "See xmclaw/plugin_sdk/AGENTS.md."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
