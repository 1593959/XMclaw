"""Symbol extraction — language-aware outline of a source file.

Strategy
--------
* **Python** — ``ast`` (stdlib). Zero dependencies, exact.
* **Other languages** — lightweight regex fallback. Good enough for
  chunking and search; not a compiler.

The public surface is :func:`extract_symbols`, which returns a list of
:class:`Symbol` objects regardless of language.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


SymbolKind = Literal["module", "class", "function", "method", "interface", "type", "variable", "unknown"]


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    kind: SymbolKind
    start_line: int          # 1-based, inclusive
    end_line: int            # 1-based, inclusive
    docstring: str | None
    signature: str | None    # e.g. "def foo(a: int) -> str"


# ---------------------------------------------------------------------------
# Python — ast
# ---------------------------------------------------------------------------

def _py_docstring(node: ast.AST) -> str | None:
    """Return the docstring of an AST node, or None."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
        return None
    body = node.body
    if not body:
        return None
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        return first.value.value
    return None


def _py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Reconstruct a rough signature string from an AST function node."""
    args = node.args
    parts: list[str] = []
    # positional / positional-only
    defaults_offset = len(args.args) - len(args.defaults)
    for i, arg in enumerate(args.args):
        arg_str = arg.arg
        if arg.annotation:
            arg_str += f": {ast.unparse(arg.annotation)}"
        if i >= defaults_offset:
            default = args.defaults[i - defaults_offset]
            arg_str += f"={ast.unparse(default)}"
        parts.append(arg_str)
    # *args
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    # kw-only
    kw_defaults_offset = len(args.kwonlyargs) - len(args.kw_defaults)
    for i, arg in enumerate(args.kwonlyargs):
        arg_str = arg.arg
        if arg.annotation:
            arg_str += f": {ast.unparse(arg.annotation)}"
        if i >= kw_defaults_offset and args.kw_defaults[i - kw_defaults_offset] is not None:
            arg_str += f"={ast.unparse(args.kw_defaults[i - kw_defaults_offset])}"
        parts.append(arg_str)
    # **kwargs
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    sig = f"def {node.name}({', '.join(parts)})"
    if node.returns:
        sig += f" -> {ast.unparse(node.returns)}"
    return sig


def _extract_python(text: str) -> list[Symbol]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    symbols: list[Symbol] = []
    mod_end = text.count("\n") + 1
    symbols.append(Symbol(
        name="<module>",
        kind="module",
        start_line=1,
        end_line=mod_end,
        docstring=_py_docstring(tree),
        signature=None,
    ))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parent = getattr(node, "parent", None)
            kind: SymbolKind = "method" if isinstance(parent, ast.ClassDef) else "function"
            symbols.append(Symbol(
                name=node.name,
                kind=kind,
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                docstring=_py_docstring(node),
                signature=_py_signature(node),
            ))
        elif isinstance(node, ast.ClassDef):
            symbols.append(Symbol(
                name=node.name,
                kind="class",
                start_line=node.lineno,
                end_line=getattr(node, "end_lineno", node.lineno),
                docstring=_py_docstring(node),
                signature=None,
            ))

    # Sort by line number for deterministic output.
    symbols.sort(key=lambda s: (s.start_line, s.name))
    return symbols


def _inject_parents(tree: ast.AST) -> None:
    """Attach ``.parent`` references so _extract_python can detect methods."""
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            setattr(child, "parent", node)


# ---------------------------------------------------------------------------
# Regex fallback patterns for common languages
# ---------------------------------------------------------------------------

# Language → (pattern, kind, is_block)
# Each pattern should capture the symbol name in group 1.
REGEX_PATTERNS: dict[str, list[tuple[re.Pattern[str], SymbolKind, bool]]] = {
    ".js": [
        (re.compile(r"^\s*function\s+(\w+)\s*\("), "function", True),
        (re.compile(r"^\s*class\s+(\w+)"), "class", True),
        (re.compile(r"^\s*const\s+(\w+)\s*=\s*(?:async\s*)?\("), "function", True),
        (re.compile(r"^\s*export\s+(?:default\s+)?(?:function|class)\s+(\w+)"), "function", True),
    ],
    ".ts": [
        (re.compile(r"^\s*function\s+(\w+)\s*\("), "function", True),
        (re.compile(r"^\s*class\s+(\w+)"), "class", True),
        (re.compile(r"^\s*interface\s+(\w+)"), "interface", True),
        (re.compile(r"^\s*type\s+(\w+)\s*="), "type", True),
        (re.compile(r"^\s*export\s+(?:default\s+)?(?:function|class|interface|type)\s+(\w+)"), "function", True),
    ],
    ".go": [
        (re.compile(r"^\s*func\s+(?:\([^)]+\)\s+)?(\w+)\s*\("), "function", True),
        (re.compile(r"^\s*type\s+(\w+)\s+(?:struct|interface)"), "class", True),
    ],
    ".rs": [
        (re.compile(r"^\s*fn\s+(\w+)\s*\("), "function", True),
        (re.compile(r"^\s*struct\s+(\w+)"), "class", True),
        (re.compile(r"^\s*trait\s+(\w+)"), "interface", True),
        (re.compile(r"^\s*impl\s+(?:<[^>]+>\s*)?(\w+)"), "class", True),
    ],
    ".java": [
        (re.compile(r"^\s*(?:public\s+|private\s+|protected\s+)?(?:static\s+)?(?:class|interface|enum)\s+(\w+)"), "class", True),
        (re.compile(r"^\s*(?:public\s+|private\s+|protected\s+)?(?:static\s+)?[\w<>,\s]+\s+(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w,\s]+)?\s*\{"), "function", True),
    ],
    ".c": [
        (re.compile(r"^\s*(?:static\s+)?(?:inline\s+)?[\w\s*]+\s+(\w+)\s*\([^)]*\)\s*\{"), "function", True),
    ],
    ".cpp": [
        (re.compile(r"^\s*(?:class|struct)\s+(\w+)"), "class", True),
        (re.compile(r"^\s*(?:static\s+)?(?:inline\s+)?(?:virtual\s+)?[\w\s*:<>,]+\s+(\w+)\s*\([^)]*\)\s*(?:const\s*)?(?:override\s*)?(?:=\s*0\s*)?\{"), "function", True),
    ],
}


def _extract_regex(text: str, ext: str) -> list[Symbol]:
    """Regex-based symbol extraction for non-Python languages."""
    patterns = REGEX_PATTERNS.get(ext.lower(), [])
    if not patterns:
        return []

    lines = text.splitlines()
    symbols: list[Symbol] = []
    # Insert module pseudo-symbol
    symbols.append(Symbol(
        name="<module>",
        kind="module",
        start_line=1,
        end_line=len(lines),
        docstring=None,
        signature=None,
    ))

    for i, line in enumerate(lines, start=1):
        for pat, kind, is_block in patterns:
            m = pat.match(line)
            if m:
                name = m.group(1)
                # Crude end-line: scan forward for a line that looks like
                # the end of a block (same or lower indentation, or closing brace).
                end_line = i
                if is_block:
                    indent = len(line) - len(line.lstrip())
                    brace_depth = line.count("{") - line.count("}")
                    for j in range(i + 1, len(lines) + 1):
                        sub = lines[j - 1]
                        brace_depth += sub.count("{") - sub.count("}")
                        if brace_depth <= 0:
                            end_line = j
                            break
                        # Heuristic: a line at same or lower indent that is not
                        # blank and not inside braces signals end.
                        sub_indent = len(sub) - len(sub.lstrip())
                        if sub.strip() and sub_indent <= indent and brace_depth == 0:
                            end_line = j - 1
                            break
                        end_line = j
                symbols.append(Symbol(
                    name=name,
                    kind=kind,
                    start_line=i,
                    end_line=end_line,
                    docstring=None,
                    signature=line.strip()[:200] or None,
                ))
                break  # first match wins for this line

    symbols.sort(key=lambda s: (s.start_line, s.name))
    return symbols


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

def extract_symbols(text: str, path: Path) -> list[Symbol]:
    """Return symbols found in *text* (language inferred from *path* suffix).

    Returns an empty list on unparseable / unsupported files.
    """
    ext = path.suffix.lower()
    if ext == ".py":
        try:
            tree = ast.parse(text)
            _inject_parents(tree)
            return _extract_python(text)
        except SyntaxError:
            return []
    return _extract_regex(text, ext)
