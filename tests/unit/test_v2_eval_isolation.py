"""Eval memory isolation — benchmark runs must NOT pollute ~/.xmclaw.

2026-06-17 regression: LongMemEval's synthetic fixtures ("User: I have a
golden retriever and I work as a dentist.") flowed through the agent's
memory pipeline into the user's real memory stores and then surfaced in
real chat as fabricated user facts. ``xmclaw eval`` now points
``XMC_DATA_DIR`` at a throwaway home so every store is isolated.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xmclaw.cli.eval import _build_agent_factory


def test_real_factory_isolates_xmc_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XMC_DATA_DIR", raising=False)
    cfg = {"llm": {"anthropic": {"api_key": "k"}}}

    _build_agent_factory(cfg, None)

    import os
    iso = os.environ.get("XMC_DATA_DIR")
    assert iso, "eval must set XMC_DATA_DIR to isolate memory"
    # It must NOT be the user's real home.
    assert Path(iso) != Path.home() / ".xmclaw"
    assert "eval" in iso.lower()
    # data_dir() now resolves to the isolated home.
    from xmclaw.utils.paths import data_dir
    assert data_dir() == Path(iso)


def test_respects_user_pinned_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    # If the operator already pinned XMC_DATA_DIR, don't override it.
    monkeypatch.setenv("XMC_DATA_DIR", "/tmp/my-pinned-home")
    cfg = {"llm": {"anthropic": {"api_key": "k"}}}
    _build_agent_factory(cfg, None)
    import os
    assert os.environ.get("XMC_DATA_DIR") == "/tmp/my-pinned-home"


def test_stub_factory_does_not_set_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    # No config → stub agent, no real memory, no env mutation.
    monkeypatch.delenv("XMC_DATA_DIR", raising=False)
    _build_agent_factory(None, None)
    import os
    assert os.environ.get("XMC_DATA_DIR") is None
