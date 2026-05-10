"""DEPRECATED — abandoned external refactor (2026-05-10).

This file was created by a botched mass-refactor attempting to
extract ``_lifespan`` out of ``xmclaw/daemon/app.py``. The lifespan
logic actually stays inline in ``app.py``; this stub exists only so
the Patch A regression test (``tests/unit/test_v2_paths_unified.py``)
doesn't trip on the broken hand-built path strings the corruption
left behind.

Safe to delete in a follow-up commit when sandbox permissions allow
``rm`` of pre-existing untracked files.
"""
from __future__ import annotations

# Intentionally empty.
