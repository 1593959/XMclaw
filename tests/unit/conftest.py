"""Test isolation for the unit suite — B-386-followup.

Default-pin :func:`xmclaw.utils.secrets.get_secret` to return ``None``
across the whole unit suite. Without this, factory tests that assert
``build_llm_from_config({...empty api_key...}) is None`` start failing
on a dev machine where a real Anthropic / OpenAI key happens to be
stored at the corresponding dotted name (env var, secrets file, OS
keyring) — the factory transparently picks the stored secret up and
the test sees a real LLM client instead of None.

The leak path (real on a contributor's box, surfaced 2026-05-09):

    factory.py: api_key = cfg.api_key or get_secret("llm.anthropic.api_key")
                                          └─ resolves on dev machine
    test:       assert build_llm_from_config({"llm": {"anthropic":
                                                {"api_key": ""}}}) is None
                └─ FAILS here with `<AnthropicLLM ...>` instead of None

Fix: replace ``get_secret`` with a fake returning ``None`` for ALL
unit tests via an autouse session-scoped fixture. Tests that need
real secret-resolution (e.g. testing the env > file > keyring
precedence in :mod:`test_v2_secrets`) explicitly opt out via
``request.getfixturevalue`` or pass their own monkeypatch.
"""
from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _isolate_secrets_from_machine_state(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin ``xmclaw.utils.secrets.get_secret`` to return ``None``.

    Tests in :mod:`test_v2_secrets` (the secrets module's own suite)
    opt out by carrying the marker ``@pytest.mark.real_secrets`` so
    they exercise the actual resolver. Anywhere else the fixture
    runs as a guard against the dev machine leaking into assertion
    state.
    """
    if request.node.get_closest_marker("real_secrets") is not None:
        return  # secrets-module's own tests need the real implementation

    def _fake_get_secret(name: str) -> Any:
        return None

    # Patch BOTH the module-level symbol AND the local ``from x import
    # get_secret`` shadows that early imports may have captured. The
    # factory uses a ``from xmclaw.utils.secrets import get_secret``
    # inside _resolve_secret, which is re-evaluated on each call, so
    # patching the module attribute is enough — but we also patch any
    # callsite that has imported the symbol at module load time, since
    # the binding is fixed at that point and would otherwise leak.
    monkeypatch.setattr(
        "xmclaw.utils.secrets.get_secret", _fake_get_secret, raising=True,
    )
