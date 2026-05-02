"""B-182 — extractor prompt loader.

Pins:
  * Missing file → return default + seed file with default
  * File present → return file contents (overrides default)
  * Empty file → fall back to default
  * Unreadable file → fall back to default
  * Cache invalidates on mtime change (hot reload)
  * reset_cache() clears between test runs
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from xmclaw.daemon import extractor_prompts


@pytest.fixture(autouse=True)
def _isolate_prompts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Each test gets its own prompts dir + clean cache."""
    monkeypatch.setattr(
        extractor_prompts, "prompts_dir",
        lambda: tmp_path / "extractor_prompts",
    )
    extractor_prompts.reset_cache()
    yield
    extractor_prompts.reset_cache()


def test_missing_file_returns_default_and_seeds(tmp_path: Path) -> None:
    out = extractor_prompts.load_prompt(
        "skill_extractor", "DEFAULT PROMPT BODY",
    )
    assert out == "DEFAULT PROMPT BODY"
    seeded = (tmp_path / "extractor_prompts" / "skill_extractor.md")
    assert seeded.is_file()
    assert seeded.read_text(encoding="utf-8") == "DEFAULT PROMPT BODY"


def test_existing_file_overrides_default(tmp_path: Path) -> None:
    pdir = tmp_path / "extractor_prompts"
    pdir.mkdir()
    target = pdir / "skill_extractor.md"
    target.write_text("USER-EDITED PROMPT", encoding="utf-8")

    out = extractor_prompts.load_prompt("skill_extractor", "DEFAULT")
    assert out == "USER-EDITED PROMPT"


def test_empty_file_falls_back_to_default(tmp_path: Path) -> None:
    pdir = tmp_path / "extractor_prompts"
    pdir.mkdir()
    (pdir / "x.md").write_text("   \n  \n", encoding="utf-8")
    assert extractor_prompts.load_prompt("x", "DEFAULT") == "DEFAULT"


def test_cache_hit_avoids_disk(tmp_path: Path) -> None:
    pdir = tmp_path / "extractor_prompts"
    pdir.mkdir()
    target = pdir / "y.md"
    target.write_text("v1", encoding="utf-8")

    a = extractor_prompts.load_prompt("y", "default")
    b = extractor_prompts.load_prompt("y", "default")
    assert a == b == "v1"


def test_mtime_change_invalidates_cache(tmp_path: Path) -> None:
    pdir = tmp_path / "extractor_prompts"
    pdir.mkdir()
    target = pdir / "z.md"
    target.write_text("first", encoding="utf-8")
    os.utime(target, (1000.0, 1000.0))
    assert extractor_prompts.load_prompt("z", "default") == "first"

    # Edit the file with a later mtime.
    target.write_text("second — edited", encoding="utf-8")
    os.utime(target, (2000.0, 2000.0))
    # Same path, same load_prompt call — must pick up edit.
    assert extractor_prompts.load_prompt("z", "default") == "second — edited"


def test_seed_failure_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the prompts dir is somehow unwritable, load_prompt still
    returns the in-memory default rather than raising."""
    pdir = tmp_path / "extractor_prompts"
    monkeypatch.setattr(
        extractor_prompts, "prompts_dir", lambda: pdir,
    )

    # Patch Path.write_text to raise — simulates read-only disk.
    real_write = Path.write_text

    def boom(self, *a, **k):
        if "extractor_prompts" in str(self):
            raise OSError("simulated read-only disk")
        return real_write(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", boom)
    assert extractor_prompts.load_prompt("x", "FALLBACK") == "FALLBACK"


def test_reset_cache_clears_state(tmp_path: Path) -> None:
    pdir = tmp_path / "extractor_prompts"
    pdir.mkdir()
    target = pdir / "q.md"
    target.write_text("first", encoding="utf-8")
    assert extractor_prompts.load_prompt("q", "default") == "first"

    # Reset, edit on disk WITHOUT touching mtime past the cache
    # threshold — reset_cache forces a fresh disk read.
    target.write_text("after-reset", encoding="utf-8")
    extractor_prompts.reset_cache()
    assert extractor_prompts.load_prompt("q", "default") == "after-reset"
