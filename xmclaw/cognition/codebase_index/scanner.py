"""Codebase file scanner — discover source files with git awareness.

Uses ``git ls-files`` when inside a git repo (fast, respects
``.gitignore``). Falls back to ``os.walk`` + glob for non-git
projects or when git is unavailable.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# 70+ recognised code extensions, grouped by family for optional filtering.
CODE_EXTENSIONS: set[str] = {
    # Python
    ".py", ".pyi", ".pyw",
    # JavaScript / TypeScript
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    # Web
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    # Java / JVM
    ".java", ".kt", ".scala", ".groovy", ".clj",
    # C family
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".rs", ".go",
    # Dotnet
    ".cs", ".fs", ".vb",
    # Mobile
    ".swift", ".m", ".mm", ".dart",
    # Data / Config
    ".sql", ".yaml", ".yml", ".json", ".toml", ".xml",
    # Docs / Markdown (often contain architecture decisions)
    ".md", ".rst", ".txt",
    # Shell / Ops
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".Makefile",
    # Other
    ".rb", ".php", ".pl", ".lua", ".r", ".nim", ".elm", ".erl",
    ".ex", ".exs", ".hs", ".ml", ".mli", ".jl", ".cr",
}

# Directories to always skip (like `.gitignore` defaults).
SKIP_DIRS: set[str] = {
    ".git", ".svn", ".hg",
    "node_modules", "vendor", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".venv", "venv", ".env",
    "dist", "build", "target", ".tox", ".eggs", "*.egg-info",
    ".coverage", "htmlcov", ".nox", ".pdm-build",
    # IDE
    ".idea", ".vscode", ".vs",
    # OS
    ".DS_Store", "Thumbs.db",
}

# Files to always skip.
SKIP_FILES: set[str] = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Pipfile.lock", "poetry.lock", "uv.lock",
    ".gitignore", ".gitattributes",
}


@dataclass(frozen=True, slots=True)
class FileEntry:
    """A single discovered source file."""
    path: Path          # absolute path
    relpath: str        # relative to project root, POSIX separators
    size: int
    mtime: float


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".") and name not in {".github", ".ci"}


def _should_skip_file(path: Path) -> bool:
    if path.name in SKIP_FILES:
        return True
    if path.suffix.lower() not in CODE_EXTENSIONS:
        return True
    # Skip minified / bundled artefacts by heuristic.
    name = path.name.lower()
    if ".min." in name or ".bundle." in name or ".chunk." in name:
        return True
    return False


def scan_git(root: Path) -> Iterable[FileEntry]:
    """Use ``git ls-files`` to list tracked files."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            text=False,
            check=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return

    raw = result.stdout
    if not raw:
        return

    # git ls-files -z produces null-terminated paths
    for rel_bytes in raw.split(b"\x00"):
        if not rel_bytes:
            continue
        rel = rel_bytes.decode("utf-8", errors="replace")
        abs_path = root / rel
        if _should_skip_file(abs_path):
            continue
        try:
            stat = abs_path.stat()
        except OSError:
            continue
        yield FileEntry(
            path=abs_path,
            relpath=rel.replace(os.sep, "/"),
            size=stat.st_size,
            mtime=stat.st_mtime,
        )


def scan_walk(root: Path) -> Iterable[FileEntry]:
    """Fallback ``os.walk`` scan with heuristic filtering."""
    for dirpath, dirnames, filenames in os.walk(root):
        # Mutate dirnames in-place to prune descent.
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        for name in filenames:
            abs_path = Path(dirpath) / name
            if _should_skip_file(abs_path):
                continue
            try:
                stat = abs_path.stat()
            except OSError:
                continue
            rel = abs_path.relative_to(root).as_posix()
            yield FileEntry(
                path=abs_path,
                relpath=rel,
                size=stat.st_size,
                mtime=stat.st_mtime,
            )


def scan(root: Path | str, *, prefer_git: bool = True) -> list[FileEntry]:
    """Discover source files under *root*.

    Parameters
    ----------
    root : Path | str
        Project root directory.
    prefer_git : bool
        If ``True`` and *root* is inside a git repo, use
        ``git ls-files`` (respects ``.gitignore``).

    Returns
    -------
    list[FileEntry]
        Sorted by ``relpath`` for deterministic output.
    """
    root = Path(root).resolve()
    is_git = (root / ".git").is_dir()

    if prefer_git and is_git:
        git_results = list(scan_git(root))
        if git_results:
            git_results.sort(key=lambda e: e.relpath)
            return git_results
        # git ls-files can return empty (e.g. bare worktree); fall through.

    results = list(scan_walk(root))
    results.sort(key=lambda e: e.relpath)
    return results
