"""B-369 (Sprint 1): Feishu / Lark WS reconnect on transport drop.

Pre-B-369 daemon.log showed ``[Lark] receive message loop exit, err: no
close frame received or sent`` 1-3 times/day. After each one the
adapter's ws-thread returned cleanly + ``_runner`` exited + the
asyncio task completed → adapter LOOKED running but no events flowed.
User discovered hours later when 飞书 messages didn't get replies.

This test pins the new behavior: when ``ws_client.start()`` returns or
raises, the runner rebuilds the client and starts again with capped
exponential backoff. Cancellation (daemon shutdown) propagates cleanly.

The existing test suite (``test_v2_feishu_dedup``, ``test_v2_feishu_card``,
``test_v2_feishu_injection_policy``) covers the inbound / outbound /
dedup paths separately. This file focuses on the lifetime / reconnect
contract.
"""
from __future__ import annotations

import asyncio
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest


# ── lark-oapi stub ────────────────────────────────────────────────
#
# The real lark-oapi requires network access at import time + ships a
# heavyweight transport. Inject a minimal stub so the adapter's
# ``import lark_oapi`` lines resolve to controllable fakes without
# pulling the dependency into the test environment.


def _install_lark_stub(start_behavior_factory) -> None:
    """Build a tiny stub package tree under ``sys.modules['lark_oapi']``.

    ``start_behavior_factory`` produces the ``Client.start`` impl —
    different test cases inject different drop / fail patterns.
    """
    lark = types.ModuleType("lark_oapi")
    lark_ws = types.ModuleType("lark_oapi.ws")
    lark_ws_client = types.ModuleType("lark_oapi.ws.client")
    lark_ws_client.loop = None
    lark_api = types.ModuleType("lark_oapi.api")
    lark_api_im = types.ModuleType("lark_oapi.api.im")
    lark_api_im_v1 = types.ModuleType("lark_oapi.api.im.v1")

    # Client.builder() chain — the adapter does
    # lark.Client.builder().app_id().app_secret().build()
    class _ClientBuilder:
        def app_id(self, _x: str) -> "_ClientBuilder":  # noqa: D401
            return self
        def app_secret(self, _x: str) -> "_ClientBuilder":
            return self
        def build(self) -> Any:
            return MagicMock(name="lark.Client")

    class _Client:
        @staticmethod
        def builder() -> "_ClientBuilder":
            return _ClientBuilder()

    lark.Client = _Client
    lark.LogLevel = types.SimpleNamespace(WARNING="WARNING")

    # EventDispatcherHandler.builder("", "").register_*().build()
    class _Dispatcher:
        def register_p2_im_message_receive_v1(self, _f: Any) -> "_Dispatcher":
            return self
        def build(self) -> Any:
            return MagicMock(name="dispatcher")

    class _EventDispatcherHandler:
        @staticmethod
        def builder(_a: str, _b: str) -> "_Dispatcher":
            return _Dispatcher()

    lark.EventDispatcherHandler = _EventDispatcherHandler

    # ws.Client(app_id, app_secret, event_handler=, log_level=) + .start()
    class _WsClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self._start_calls = 0

        def start(self) -> None:
            start_behavior_factory(self)

    ws_namespace = types.SimpleNamespace(Client=_WsClient)
    lark.ws = ws_namespace

    # P2ImMessageReceiveV1 placeholder — unused by reconnect tests.
    lark_api_im_v1.P2ImMessageReceiveV1 = type("P2ImMessageReceiveV1", (), {})

    # Wire packages so ``import lark_oapi`` and
    # ``from lark_oapi.api.im.v1 import P2ImMessageReceiveV1`` work.
    sys.modules["lark_oapi"] = lark
    sys.modules["lark_oapi.ws"] = lark_ws
    sys.modules["lark_oapi.ws.client"] = lark_ws_client
    sys.modules["lark_oapi.api"] = lark_api
    sys.modules["lark_oapi.api.im"] = lark_api_im
    sys.modules["lark_oapi.api.im.v1"] = lark_api_im_v1


@pytest.fixture(autouse=True)
def _restore_lark_modules() -> Any:
    """Pop the stub after each test so other test files using a
    different lark fake (or none) aren't poisoned by ours."""
    yield
    for k in list(sys.modules.keys()):
        if k.startswith("lark_oapi"):
            sys.modules.pop(k, None)


# ── tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_b369_runner_reconnects_after_clean_return() -> None:
    """``ws_client.start()`` returning normally (the daemon.log
    'receive message loop exit, no close frame' pattern) must trigger
    a reconnect, not exit the runner. ``start()`` returns immediately
    so the runner spins through the backoff + retry path quickly.
    """
    call_count = {"n": 0}

    def behavior(_client: Any) -> None:
        call_count["n"] += 1
        # Return immediately every call — runner should keep going.

    _install_lark_stub(lambda c: behavior(c))

    from xmclaw.providers.channel.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter({
        "app_id": "cli_stub_app",
        "app_secret": "stub_secret",
    })
    await adapter.start()
    # Let it loop ≥3 times: start → backoff 1s → start → backoff 2s → start
    await asyncio.sleep(2.5)
    assert call_count["n"] >= 2, (
        f"runner only invoked start() {call_count['n']} times — "
        "B-369 reconnect didn't fire"
    )
    await adapter.stop()


@pytest.mark.asyncio
async def test_b369_runner_reconnects_after_exception() -> None:
    """``ws_client.start()`` RAISING (e.g. ConnectionError mid-handshake)
    must also trigger reconnect, same path as the clean-return case."""
    call_count = {"n": 0}

    def behavior(_client: Any) -> None:
        call_count["n"] += 1
        raise ConnectionError(f"simulated drop {call_count['n']}")

    _install_lark_stub(lambda c: behavior(c))

    from xmclaw.providers.channel.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter({
        "app_id": "cli_stub_app",
        "app_secret": "stub_secret",
    })
    await adapter.start()
    await asyncio.sleep(2.5)
    assert call_count["n"] >= 2
    await adapter.stop()


@pytest.mark.asyncio
async def test_b369_runner_propagates_cancellation_during_backoff() -> None:
    """Stop during the backoff sleep must cancel cleanly — daemon
    shutdown should not get stuck in a 60s sleep waiting to retry."""
    def behavior(_client: Any) -> None:
        return  # immediate return triggers backoff

    _install_lark_stub(lambda c: behavior(c))

    from xmclaw.providers.channel.feishu.adapter import FeishuAdapter

    adapter = FeishuAdapter({
        "app_id": "cli_stub_app",
        "app_secret": "stub_secret",
    })
    await adapter.start()
    # Let it loop a few times so it accumulates backoff.
    await asyncio.sleep(1.5)
    # Stop should return promptly — backoff sleep is cancelled.
    import time as _t
    t0 = _t.monotonic()
    await adapter.stop()
    elapsed = _t.monotonic() - t0
    assert elapsed < 5.0, (
        f"adapter.stop() took {elapsed:.1f}s — B-369 backoff "
        "isn't cooperating with cancellation"
    )
