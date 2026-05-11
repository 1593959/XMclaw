"""FileWatcher._should_ignore — pin the path-segment matching contract.

Real bug surfaced 2026-05-10 via screenshot: the cognition page's
``Attention focus`` panel was full of paths like
``C:\\Users\\15978\\Desktop\\XMclaw\\.git\\logs\\refs\\remotes\\origin\\main``
even though ``.git`` is in the default ``ignore_patterns`` list.

Root cause:
  ``_should_ignore`` did ``fnmatch.fnmatch(path, pattern)`` against
  the FULL path. fnmatch does NOT do substring matching — pattern
  ``.git`` only matches a string equal to ``.git``. The full path
  contains ``.git`` as a SEGMENT but never matches ``.git`` as the
  whole string. The basename branch (``Path(path).name``) returned
  ``main``, also not matching ``.git``. So every file under ``.git/``
  leaked through to the FileWatcher → CognitiveState → UI pipeline.

Fix:
  Match against ``Path(path).parts`` so ``.git`` matches as a
  segment. Keep the basename match for wildcard patterns
  (``*.pyc``, ``*.tmp``).

These tests pin the fix so a future "let's simplify the ignore
matching" refactor can't silently regress the screen back to
streaming git internals.
"""
from __future__ import annotations


from xmclaw.cognition.file_watcher import FileWatcher


def _make_watcher(patterns: list[str]) -> FileWatcher:
    """Build a watcher with custom ignore_patterns; watch_paths is
    irrelevant for these unit tests (we only exercise _should_ignore)."""
    return FileWatcher(
        watch_paths=["/tmp/__nonexistent__"],
        ignore_patterns=patterns,
    )


def test_dotgit_inside_path_is_ignored() -> None:
    """REGRESSION: ``.git`` appearing as a path segment must be
    ignored. Pre-fix this returned False because fnmatch only
    compared to the full path string."""
    w = _make_watcher([".git"])
    assert w._should_ignore(
        "C:/Users/15978/Desktop/XMclaw/.git/logs/refs/remotes/origin/main",
    ), "the original screenshot bug — .git/ contents leaked to UI"
    assert w._should_ignore(
        "C:\\Users\\15978\\Desktop\\XMclaw\\.git\\AUTO_MERGE",
    ), "Windows path with backslashes also matches"
    assert w._should_ignore(
        "/home/u/proj/.git/index",
    ), "POSIX path with .git segment also matches"


def test_dotgit_directory_itself_is_ignored() -> None:
    """The bare ``.git`` directory (basename match) must also be
    ignored — pinned because the original code's basename branch
    DID work for this case, but the rewrite must keep the behaviour."""
    w = _make_watcher([".git"])
    assert w._should_ignore("/home/u/proj/.git")
    assert w._should_ignore("/home/u/.git")


def test_pycache_segment_ignored() -> None:
    """``__pycache__`` is one of the default patterns; same
    segment-match rule applies."""
    w = _make_watcher(["__pycache__"])
    assert w._should_ignore(
        "/home/u/proj/xmclaw/foo/__pycache__/bar.cpython-310.pyc",
    )


def test_xmclaw_workspace_ignored() -> None:
    """``.xmclaw`` (the daemon's workspace under the user's home) is
    in the default patterns — the FileWatcher would otherwise dump
    every events.db / memory.db rotation event into attention."""
    w = _make_watcher([".xmclaw"])
    assert w._should_ignore(
        "C:/Users/15978/.xmclaw/v2/events.db-wal",
    )


def test_wildcard_basename_still_works() -> None:
    """``*.pyc`` is a wildcard — the basename branch MUST still
    catch this. The old behaviour pre-fix worked for wildcards;
    the rewrite mustn't break it."""
    w = _make_watcher(["*.pyc"])
    assert w._should_ignore("/home/u/proj/foo/__pycache__/bar.pyc")
    # Path inside .pyc dir — wildcard only matches basename, but
    # basename here is "bar.pyc" so it matches.
    assert not w._should_ignore("/home/u/proj/foo/source.py")


def test_normal_path_not_ignored() -> None:
    """Sanity: real source files MUST pass through. Otherwise the
    FileWatcher silently drops everything."""
    w = _make_watcher([".git", "__pycache__", "*.pyc"])
    assert not w._should_ignore("/home/u/proj/xmclaw/daemon/agent_loop.py")
    assert not w._should_ignore(
        "C:/Users/15978/Desktop/project/src/main.py",
    )


def test_partial_segment_match_does_not_misfire() -> None:
    """``.git`` must NOT match ``.github`` — they differ as full
    segments. fnmatch literal exact-match handles this naturally."""
    w = _make_watcher([".git"])
    # .github segment must NOT be ignored — it's a real directory we
    # often want to watch (workflows changes).
    assert not w._should_ignore("/home/u/proj/.github/workflows/ci.yml")
    # gitignore filename must NOT be ignored either.
    assert not w._should_ignore("/home/u/proj/.gitignore")


def test_default_patterns_include_typical_noise() -> None:
    """Pin the default ignore list so we don't accidentally drop
    coverage of the standard noise sources. Each entry is a real
    path that surfaced in the screenshot bug."""
    w = FileWatcher(watch_paths=["/tmp/__nonexistent__"])
    # All 8 default patterns: .git, __pycache__, .xmclaw,
    # node_modules, .venv, *.pyc, .ruff_cache, .mypy_cache
    samples = [
        "/p/.git/HEAD",
        "/p/foo/__pycache__/bar.cpython-310.pyc",
        "/home/.xmclaw/v2/memory.db",
        "/p/node_modules/foo/index.js",
        "/p/.venv/bin/python",
        "/p/build/foo.pyc",
        "/p/.ruff_cache/CACHEDIR.TAG",
        "/p/.mypy_cache/.gitignore",
        "/p/.pytest_cache/v/cache/nodeids",
    ]
    for s in samples:
        assert w._should_ignore(s), (
            f"default pattern set failed to ignore {s!r} — "
            "would leak into attention focus"
        )


def test_pytest_cache_segment_ignored() -> None:
    """``.pytest_cache`` is in the default ignore list — was the
    original screenshot's #2 noise source after .git/."""
    w = FileWatcher(watch_paths=["/tmp/__nonexistent__"])
    assert w._should_ignore(
        "C:/Users/15978/Desktop/XMclaw/.pytest_cache/v/cache/nodeids",
    )
    assert w._should_ignore(
        "C:/Users/15978/Desktop/XMclaw/.pytest_cache/v/cache/lastfailed",
    )
