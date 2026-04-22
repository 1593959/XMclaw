"""Epic #15 phase 2 regression guard — no bare ``print()`` in logging contexts.

User-facing CLI output (``xmclaw chat``, ``xmclaw doctor``, interactive
prompts) legitimately uses ``print``/``typer.echo`` — that's UX, not
logging. Everything else has to go through ``xmclaw.utils.log.get_logger``
so it picks up:

  * structured JSON output (machine parseable),
  * the secret scrubber (no stray API keys in logs),
  * contextvar merge (session_id / agent_id / tool_id).

This test walks the AST of every module under the "logging-required"
subtrees and fails if it finds a ``print(...)`` call. A top-level comment
starting with ``# print-audit: allow`` on the same line is the escape
hatch for the rare legitimate case (e.g. a debug-only helper).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PKG = _ROOT / "xmclaw"

# CLI and plugins are exempt: cli/ emits user-facing text, plugins/ is
# third-party territory we don't control. Every other subtree speaks
# to logs, not stdout.
_AUDITED_SUBTREES = [
    _PKG / "core",
    _PKG / "providers",
    _PKG / "daemon",
    _PKG / "security",
    _PKG / "skills",
    _PKG / "utils",
    _PKG / "memory",
    _PKG / "runtime",
]


def _collect_files() -> list[Path]:
    files: list[Path] = []
    for root in _AUDITED_SUBTREES:
        if not root.exists():
            continue
        files.extend(sorted(root.rglob("*.py")))
    return files


def _violations_in_file(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return [(0, "syntax error parsing file")]
    # Build a lookup of escape-hatch lines.
    allow_lines: set[int] = set()
    for lineno, line in enumerate(src.splitlines(), start=1):
        if "print-audit: allow" in line:
            allow_lines.add(lineno)
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "print" and node.lineno not in allow_lines:
                hits.append((node.lineno, ast.unparse(node)))
    return hits


def test_no_bare_print_in_logging_subtrees() -> None:
    """Every subtree that speaks to logs must route through get_logger.

    If this fails, the fix is one of:
      * replace the ``print(...)`` with ``_log.info(...)`` (or .warning /
        .error — pick the severity that matches) after instantiating
        ``_log = get_logger(__name__)`` at module level.
      * if the print is legitimately a debug breadcrumb you can't avoid,
        put ``# print-audit: allow`` on the same line and own it.
    """
    offenders: list[str] = []
    for path in _collect_files():
        for lineno, snippet in _violations_in_file(path):
            rel = path.relative_to(_ROOT)
            offenders.append(f"  {rel}:{lineno}  {snippet}")
    if offenders:
        pytest.fail(
            "bare print() in logging-required modules — use get_logger():\n"
            + "\n".join(offenders),
        )
