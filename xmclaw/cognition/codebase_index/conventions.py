"""Project convention extraction — infer coding style & architecture from indexed code.

Reads the already-indexed chunks for a project and produces a
``conventions.md`` snippet that can be injected into the agent's
system prompt.  This is intentionally heuristic / best-effort;
accuracy improves with more indexed files.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from xmclaw.cognition.codebase_index.store import CodebaseStore
from xmclaw.utils.log import get_logger

_log = get_logger(__name__)


@dataclass
class ProjectConventions:
    """Inferred conventions for a codebase."""
    language_primary: str | None = None
    test_framework: str | None = None
    linter: str | None = None
    line_length: int | None = None
    type_hints: str | None = None   # "strict", "loose", "none"
    async_style: str | None = None  # "asyncio", "trio", "none"
    import_style: str | None = None # "absolute", "relative", "mixed"
    architecture_pattern: str | None = None  # "mvc", "repository", "hexagonal", "none"
    key_files: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)


def _detect_python_test_framework(text_samples: list[str]) -> str | None:
    scores = {"pytest": 0, "unittest": 0, "doctest": 0}
    for text in text_samples:
        low = text.lower()
        if "def test_" in low or "import pytest" in low or "@pytest." in low:
            scores["pytest"] += 1
        if "import unittest" in low or "class Test" in low:
            scores["unittest"] += 1
        if ">>> " in low:
            scores["doctest"] += 1
    if not any(scores.values()):
        return None
    return max(scores, key=scores.get)  # type: ignore[arg-type]


def _detect_linter(text_samples: list[str]) -> str | None:
    for text in text_samples:
        if "ruff" in text:
            return "ruff"
        if "flake8" in text or "pycodestyle" in text:
            return "flake8"
        if "pylint" in text:
            return "pylint"
        if "eslint" in text:
            return "eslint"
        if "black" in text:
            return "black"
    return None


def _detect_line_length(text_samples: list[str]) -> int | None:
    """Heuristic: scan for 'line-length = N' or 'max-line-length=N'."""
    import re
    for text in text_samples:
        for m in re.finditer(r"line[_\s-]length\s*=\s*(\d+)", text, re.I):
            return int(m.group(1))
        for m in re.finditer(r"max[_\s-]line[_\s-]length\s*=\s*(\d+)", text, re.I):
            return int(m.group(1))
    return None


def _detect_type_hints(text_samples: list[str]) -> str | None:
    typed = 0
    untyped = 0
    for text in text_samples[:20]:
        for line in text.splitlines():
            if line.strip().startswith("def "):
                if "->" in line or ": " in line.split("(")[0]:
                    typed += 1
                else:
                    untyped += 1
    if typed == 0 and untyped == 0:
        return None
    ratio = typed / (typed + untyped)
    if ratio > 0.8:
        return "strict"
    if ratio > 0.3:
        return "loose"
    return "none"


def _detect_async(text_samples: list[str]) -> str | None:
    async_count = 0
    for text in text_samples:
        if "async def" in text or "await " in text:
            async_count += text.count("async def") + text.count("await ")
    if async_count > 5:
        return "asyncio"
    return "none"


def _detect_architecture(text_samples: list[str]) -> str | None:
    scores: dict[str, int] = {}
    for text in text_samples:
        low = text.lower()
        if "repository" in low:
            scores["repository"] = scores.get("repository", 0) + 1
        if "controller" in low and "model" in low:
            scores["mvc"] = scores.get("mvc", 0) + 1
        if "service" in low and "port" in low:
            scores["hexagonal"] = scores.get("hexagonal", 0) + 1
        if "router" in low and "handler" in low:
            scores["mvc"] = scores.get("mvc", 0) + 1
    if not scores:
        return None
    return max(scores, key=scores.get)  # type: ignore[arg-type]


def _guess_language(top_extensions: list[tuple[str, int]]) -> str | None:
    if not top_extensions:
        return None
    ext_map = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".java": "Java", ".go": "Go", ".rs": "Rust", ".cpp": "C++",
        ".c": "C", ".cs": "C#", ".rb": "Ruby", ".php": "PHP",
        ".swift": "Swift", ".kt": "Kotlin", ".scala": "Scala",
    }
    primary_ext, _ = top_extensions[0]
    return ext_map.get(primary_ext.lower())


def extract_conventions(store: CodebaseStore, root_path: str) -> ProjectConventions:
    """Infer conventions from indexed chunks for *root_path*."""
    prefix = root_path + "/"
    # Pull a representative sample of chunks.
    cur = store._conn.cursor()
    rows = cur.execute(
        "SELECT relpath, text, chunk_type FROM chunks WHERE relpath LIKE ? LIMIT 200",
        (prefix + "%",),
    ).fetchall()

    if not rows:
        return ProjectConventions()

    text_samples = [r["text"] for r in rows]
    relpaths = [r["relpath"] for r in rows]

    # Language distribution from file extensions.
    from collections import Counter
    exts = Counter(Path(r).suffix for r in relpaths)
    top_exts = exts.most_common(3)

    conv = ProjectConventions(
        language_primary=_guess_language(top_exts),
        test_framework=_detect_python_test_framework(text_samples),
        linter=_detect_linter(text_samples),
        line_length=_detect_line_length(text_samples),
        type_hints=_detect_type_hints(text_samples),
        async_style=_detect_async(text_samples),
        architecture_pattern=_detect_architecture(text_samples),
    )

    # Key files: look for well-known names.
    key_names = {"README", "CONTRIBUTING", "pyproject", "package", "Cargo", "Makefile", "Dockerfile"}
    key_files = []
    for r in set(relpaths):
        base = Path(r).name
        if any(base.lower().startswith(k.lower()) for k in key_names):
            key_files.append(r)
    conv.key_files = sorted(set(key_files))[:10]

    return conv


def render_conventions(conv: ProjectConventions) -> str:
    """Render conventions as a markdown snippet suitable for prompt injection."""
    lines: list[str] = ["# Project Conventions (auto-extracted)", ""]

    def _add(label: str, value: Any) -> None:
        if value is not None and value != []:
            if isinstance(value, list):
                lines.append(f"- **{label}**: {', '.join(str(v) for v in value)}")
            else:
                lines.append(f"- **{label}**: {value}")

    _add("Primary language", conv.language_primary)
    _add("Test framework", conv.test_framework)
    _add("Linter / formatter", conv.linter)
    _add("Line length", conv.line_length)
    _add("Type hints", conv.type_hints)
    _add("Async style", conv.async_style)
    _add("Architecture pattern", conv.architecture_pattern)
    _add("Key files", conv.key_files)
    _add("Dependencies", conv.dependencies)

    if len(lines) == 2:
        lines.append("_No conventions could be inferred from the current index._")

    return "\n".join(lines)
