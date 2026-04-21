"""Tool-Call IR double-direction fuzz (CI-4, anti-req #3).

Phase 2: once translators are implemented, round-trip every sampled ToolCall
through encode → decode and assert equality. A translator that can't survive
this loop is not allowed to ship.
"""
from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Phase 2 — translators not yet implemented")
def test_anthropic_translator_roundtrip() -> None:
    raise NotImplementedError


@pytest.mark.skip(reason="Phase 2 — translators not yet implemented")
def test_openai_translator_roundtrip() -> None:
    raise NotImplementedError
