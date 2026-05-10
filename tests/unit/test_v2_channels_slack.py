"""B-382 (Sprint 2): SlackAdapter unit tests.

Pre-B-382 there was no Slack adapter at all (no scaffold, no manifest).
The real adapter uses ``slack-bolt``'s Socket Mode (no public webhook
needed) + Epic #14 injection scan + the same allowlist posture telegram
+ feishu use.

Test posture mirrors :file:`test_v2_channels_telegram.py` —
duck-type Slack ``message`` events directly into ``_on_message_async``
rather than booting a real ``AsyncApp``. ``start()`` / outbound
``send()`` exercise a mock ``AsyncApp`` + ``AsyncSocketModeHandler``
to avoid network. The lazy-import exit path is verified by setting
``slack_bolt.async_app`` to ``None`` in ``sys.modules``.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xmclaw.providers.channel.base import ChannelTarget, OutboundMessage
from xmclaw.providers.channel._shared import split_text
from xmclaw.providers.channel.slack.adapter import (
    SlackAdapter,
    _coerce_str_set,
)


# ── helpers ────────────────────────────────────────────────────────


def _make_event(
    *,
    text: str = "hello",
    channel: str = "C12345",
    user: str = "U67890",
    ts: str = "1700000000.000100",
    subtype: str | None = None,
    bot_id: str | None = None,
    thread_ts: str | None = None,
    client_msg_id: str = "abc-123",
) -> dict:
    """Duck-type a slack-bolt 'message' event payload. Only the keys
    the adapter actually reads need to exist."""
    event: dict = {
        "type": "message",
        "text": text,
        "channel": channel,
        "user": user,
        "ts": ts,
        "client_msg_id": client_msg_id,
    }
    if subtype is not None:
        event["subtype"] = subtype
    if bot_id is not None:
        event["bot_id"] = bot_id
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return event


def _build_adapter(**extra_cfg) -> SlackAdapter:
    """Construct without start() — exercises only _on_message_async /
    helpers. ``bot_token`` + ``app_token`` are required at __init__ so
    we always pass them."""
    cfg = {
        "bot_token": "xoxb-fake-test-bot-token",
        "app_token": "xapp-fake-test-app-token",
    }
    cfg.update(extra_cfg)
    return SlackAdapter(cfg)


# ── construction + config validation ───────────────────────────────


def test_adapter_requires_bot_token() -> None:
    with pytest.raises(ValueError, match="bot_token"):
        SlackAdapter({"app_token": "xapp-x"})


def test_adapter_requires_app_token() -> None:
    """Socket Mode needs the app-level token (xapp-) with
    connections:write — without it no WS opens."""
    with pytest.raises(ValueError, match="app_token"):
        SlackAdapter({"bot_token": "xoxb-x"})


def test_adapter_rejects_string_allowlist() -> None:
    """A common config typo: ``allowed_user_ids: "U123"`` (string
    instead of list). Catch at __init__ rather than letting it
    silently match nothing forever."""
    with pytest.raises(ValueError, match="allowed_user_ids"):
        SlackAdapter({
            "bot_token": "xoxb-x",
            "app_token": "xapp-x",
            "allowed_user_ids": "U12345",
        })


def test_adapter_accepts_string_ids_in_allowlist() -> None:
    """Slack ids are opaque strings (Uxxx, Cxxx, Dxxx); accept them
    as-is."""
    adapter = SlackAdapter({
        "bot_token": "xoxb-x",
        "app_token": "xapp-x",
        "allowed_user_ids": ["U123", "U456"],
        "allowed_channel_ids": ["C789", "D000"],
    })
    assert adapter._allowed_user_ids == {"U123", "U456"}
    assert adapter._allowed_channel_ids == {"C789", "D000"}


def test_adapter_rejects_non_string_id_in_allowlist() -> None:
    """A ``[12345]`` typo would silently include int 12345 in a set
    that's then compared against str user ids — guaranteed mismatch."""
    with pytest.raises(ValueError, match="must be str"):
        SlackAdapter({
            "bot_token": "xoxb-x",
            "app_token": "xapp-x",
            "allowed_user_ids": [12345],
        })


def test_adapter_accepts_dispatch_session_id_prefix() -> None:
    """The config field is informational (the dispatcher composes
    session_id from adapter.name + target.ref); we still accept it
    so the setup endpoint can echo what the user configured."""
    adapter = SlackAdapter({
        "bot_token": "xoxb-x",
        "app_token": "xapp-x",
        "dispatch_session_id_prefix": "myteam-slack-",
    })
    assert adapter._session_prefix == "myteam-slack-"


# ── inbound dispatch ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_message_produces_correct_session_id() -> None:
    """The session_id used by ChannelDispatcher is f"{channel}:{ref}".
    For Slack that's "slack:<channel_id>" — the dispatcher composes
    this from inbound.target.channel + inbound.target.ref."""
    adapter = _build_adapter()
    inbox: list = []

    async def handler(msg) -> None:
        inbox.append(msg)

    adapter.subscribe(handler)
    await adapter._on_message_async(
        _make_event(text="hello", channel="C9999", user="U1234"),
    )
    assert len(inbox) == 1
    msg = inbox[0]
    assert msg.target.channel == "slack"
    assert msg.target.ref == "C9999"
    assert msg.content == "hello"
    assert msg.user_ref == "U1234"
    # Dispatcher composes "slack:C9999" from these fields.
    assert f"{msg.target.channel}:{msg.target.ref}" == "slack:C9999"


@pytest.mark.asyncio
async def test_inbound_dm_channel_dxxx() -> None:
    """DMs use 'D...' channel ids; they should flow through identically
    to public 'C...' channels."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(
        _make_event(text="dm hello", channel="D55555"),
    )
    assert len(inbox) == 1
    assert inbox[0].target.ref == "D55555"


@pytest.mark.asyncio
async def test_inbound_dedup_drops_duplicate_event() -> None:
    """Slack's Socket Mode has at-least-once delivery — same event can
    land twice on reconnect. Dedup mirrors feishu / telegram."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    ev = _make_event(text="ping", channel="C1", ts="123.456")
    await adapter._on_message_async(ev)
    await adapter._on_message_async(ev)  # exact replay
    assert inbox == ["ping"]


@pytest.mark.asyncio
async def test_inbound_skips_bot_messages() -> None:
    """Bot-authored messages must not be routed to the agent — would
    create an echo loop if the agent's reply triggers another inbound."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    # bot_id present
    await adapter._on_message_async(
        _make_event(text="echo", bot_id="B12345"),
    )
    # subtype=bot_message
    await adapter._on_message_async(
        _make_event(text="echo2", subtype="bot_message"),
    )
    assert inbox == []


@pytest.mark.asyncio
async def test_inbound_skips_message_changed_and_deleted() -> None:
    """Edits and deletions arrive as message_changed / message_deleted
    subtypes — there's no fresh user content; routing them would
    re-process stale text."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(
        _make_event(text="edited", subtype="message_changed", ts="1.1"),
    )
    await adapter._on_message_async(
        _make_event(text="gone", subtype="message_deleted", ts="1.2"),
    )
    assert inbox == []


@pytest.mark.asyncio
async def test_inbound_skips_empty_text_and_no_channel() -> None:
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    # Empty text — drop.
    await adapter._on_message_async(_make_event(text="   "))
    # No channel — drop (defensive; real Slack messages always have one).
    await adapter._on_message_async(_make_event(text="x", channel=""))
    assert inbox == []


@pytest.mark.asyncio
async def test_inbound_threading_message_id_is_thread_ts_when_set() -> None:
    """When the user posts in an existing thread, the inbound carries
    thread_ts; the dispatcher forwards it as reply_to so the agent's
    reply lands in the same thread."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(
        _make_event(text="in thread", ts="999.111", thread_ts="500.000"),
    )
    assert len(inbox) == 1
    assert inbox[0].raw["message_id"] == "500.000"


@pytest.mark.asyncio
async def test_inbound_message_id_falls_back_to_ts() -> None:
    """A top-level (non-thread) message has no thread_ts; we still
    surface its own ts as message_id so the dispatcher can thread the
    agent's reply under it."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(
        _make_event(text="root msg", ts="888.222"),
    )
    assert inbox[0].raw["message_id"] == "888.222"


# ── allowlist ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allowlist_drops_unauthorized_user() -> None:
    adapter = _build_adapter(allowed_user_ids=["U_owner"])
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    # Non-allowlisted user.
    await adapter._on_message_async(
        _make_event(text="from stranger", user="U_stranger", ts="1.0"),
    )
    assert inbox == []


@pytest.mark.asyncio
async def test_allowlist_passes_authorized_user() -> None:
    adapter = _build_adapter(allowed_user_ids=["U_owner"])
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(
        _make_event(text="from owner", user="U_owner", ts="1.1"),
    )
    assert inbox == ["from owner"]


@pytest.mark.asyncio
async def test_no_allowlist_lets_anyone_in() -> None:
    """Backward compat: no allowlist set → any sender is fine. Matches
    feishu / telegram defaults."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(
        _make_event(text="any user", user="U_random", ts="1.2"),
    )
    assert inbox == ["any user"]


@pytest.mark.asyncio
async def test_channel_allowlist_drops_unauthorized_channel() -> None:
    """A user might be allowlisted in their DM but not authorized to
    drive the agent from a public channel. allowed_channel_ids splits
    this — empty list = no channel restriction; non-empty = channel
    must be in the list."""
    adapter = _build_adapter(allowed_channel_ids=["C_team", "D_owner"])
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    # User OK, channel NOT in allowlist.
    await adapter._on_message_async(
        _make_event(text="wrong channel", channel="C_random", ts="1.3"),
    )
    assert inbox == []


# ── outbound send ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_hits_correct_channel_id() -> None:
    """send() must call client.chat_postMessage with channel=target.ref."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.chat_postMessage = AsyncMock(
        return_value={"ok": True, "ts": "1700000000.000200"},
    )
    adapter._app = SimpleNamespace(client=fake_client)

    await adapter.send(
        ChannelTarget(channel="slack", ref="C9999"),
        OutboundMessage(content="hi from agent"),
    )
    fake_client.chat_postMessage.assert_called_once()
    kwargs = fake_client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "C9999"
    assert kwargs["text"] == "hi from agent"
    assert kwargs["thread_ts"] is None  # no reply_to


@pytest.mark.asyncio
async def test_send_passes_thread_ts() -> None:
    """reply_to maps to Slack's thread_ts so replies land in the same
    thread — Slack's threading model is different from Telegram's."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.chat_postMessage = AsyncMock(
        return_value={"ok": True, "ts": "ts2"},
    )
    adapter._app = SimpleNamespace(client=fake_client)

    await adapter.send(
        ChannelTarget(channel="slack", ref="C9999"),
        OutboundMessage(content="reply text", reply_to="500.000"),
    )
    kwargs = fake_client.chat_postMessage.call_args.kwargs
    assert kwargs["thread_ts"] == "500.000"


@pytest.mark.asyncio
async def test_send_rejects_wrong_channel_target() -> None:
    """An adapter must refuse ChannelTargets for a different channel."""
    adapter = _build_adapter()
    adapter._app = SimpleNamespace(client=MagicMock())
    with pytest.raises(ValueError):
        await adapter.send(
            ChannelTarget(channel="not-slack", ref="C1"),
            OutboundMessage(content="leak"),
        )


@pytest.mark.asyncio
async def test_send_chunks_long_messages() -> None:
    """Slack's text rendering breaks ~4000 chars; long replies arrive
    as successive posts. Unlike Telegram, every chunk carries thread_ts
    so all chunks land in the same thread."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.chat_postMessage = AsyncMock(
        return_value={"ok": True, "ts": "ts-chunk"},
    )
    adapter._app = SimpleNamespace(client=fake_client)

    long_text = "word " * 1000  # 5000 chars
    await adapter.send(
        ChannelTarget(channel="slack", ref="C1"),
        OutboundMessage(content=long_text, reply_to="500.000"),
    )
    assert fake_client.chat_postMessage.await_count >= 2
    # Every chunk threads under the same parent ts.
    for call in fake_client.chat_postMessage.call_args_list:
        assert call.kwargs["thread_ts"] == "500.000"


@pytest.mark.asyncio
async def test_send_raises_on_slack_failure() -> None:
    """If chat_postMessage raises (Slack returns rate_limited / network
    blip), the adapter surfaces a RuntimeError so the dispatcher's
    outer try/except records 'channel.send_failed'."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.chat_postMessage = AsyncMock(
        side_effect=RuntimeError("rate_limited"),
    )
    adapter._app = SimpleNamespace(client=fake_client)

    with pytest.raises(RuntimeError, match="slack send failed"):
        await adapter.send(
            ChannelTarget(channel="slack", ref="C1"),
            OutboundMessage(content="will fail"),
        )


@pytest.mark.asyncio
async def test_send_unstarted_adapter_raises() -> None:
    adapter = _build_adapter()
    with pytest.raises(RuntimeError, match="not started"):
        await adapter.send(
            ChannelTarget(channel="slack", ref="C1"),
            OutboundMessage(content="x"),
        )


@pytest.mark.asyncio
async def test_send_empty_content_returns_quickly() -> None:
    """Slack's chat.postMessage rejects empty 'text' (no_text error).
    Adapter should bail before hitting the API."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.chat_postMessage = AsyncMock()
    adapter._app = SimpleNamespace(client=fake_client)

    result = await adapter.send(
        ChannelTarget(channel="slack", ref="C1"),
        OutboundMessage(content=""),
    )
    assert result == ""
    fake_client.chat_postMessage.assert_not_called()


# ── start() lifecycle ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_connects_with_configured_tokens() -> None:
    """start() must hand bot_token to AsyncApp and app_token to
    AsyncSocketModeHandler. Patches both imports so no network is
    touched + we can verify the tokens thread through."""
    adapter = _build_adapter(
        bot_token="xoxb-real-bot-token",
        app_token="xapp-real-app-token",
    )

    fake_app = MagicMock()
    fake_app.event = MagicMock(return_value=lambda fn: fn)  # decorator no-op

    fake_handler = MagicMock()
    fake_handler.connect_async = AsyncMock()
    fake_handler.close_async = AsyncMock()

    fake_async_app_cls = MagicMock(return_value=fake_app)
    fake_handler_cls = MagicMock(return_value=fake_handler)

    with patch.dict(sys.modules, {
        "slack_bolt.async_app": SimpleNamespace(AsyncApp=fake_async_app_cls),
        "slack_bolt.adapter.socket_mode.async_handler": SimpleNamespace(
            AsyncSocketModeHandler=fake_handler_cls,
        ),
    }):
        await adapter.start()

    fake_async_app_cls.assert_called_once_with(token="xoxb-real-bot-token")
    fake_handler_cls.assert_called_once()
    args, _ = fake_handler_cls.call_args
    # AsyncSocketModeHandler(app, app_token) — second positional arg is
    # the app_token.
    assert args[0] is fake_app
    assert args[1] == "xapp-real-app-token"
    fake_handler.connect_async.assert_awaited_once()
    assert adapter._app is fake_app
    assert adapter._handler is fake_handler
    assert adapter.last_start_error is None


@pytest.mark.asyncio
async def test_start_socket_mode_failure_surfaces_clear_error() -> None:
    """connect_async raises (bad app_token, Socket Mode disabled,
    network blocked). Adapter surfaces a RuntimeError with operator-
    readable text + sets last_start_error so the setup endpoint can
    render it."""
    adapter = _build_adapter()

    fake_app = MagicMock()
    fake_app.event = MagicMock(return_value=lambda fn: fn)

    fake_handler = MagicMock()
    fake_handler.connect_async = AsyncMock(
        side_effect=RuntimeError("invalid_auth"),
    )
    fake_async_app_cls = MagicMock(return_value=fake_app)
    fake_handler_cls = MagicMock(return_value=fake_handler)

    with patch.dict(sys.modules, {
        "slack_bolt.async_app": SimpleNamespace(AsyncApp=fake_async_app_cls),
        "slack_bolt.adapter.socket_mode.async_handler": SimpleNamespace(
            AsyncSocketModeHandler=fake_handler_cls,
        ),
    }):
        with pytest.raises(RuntimeError, match="Socket Mode"):
            await adapter.start()

    assert adapter.last_start_error is not None
    assert (
        "connections:write" in adapter.last_start_error
        or "Socket Mode" in adapter.last_start_error
        or "app_token" in adapter.last_start_error
    )


@pytest.mark.asyncio
async def test_start_idempotent() -> None:
    """A second start() call after success is a no-op."""
    adapter = _build_adapter()
    sentinel = MagicMock()
    adapter._app = sentinel
    await adapter.start()  # MUST NOT do anything
    assert adapter._app is sentinel


@pytest.mark.asyncio
async def test_stop_closes_socket_mode_handler_cleanly() -> None:
    adapter = _build_adapter()
    fake_handler = MagicMock()
    fake_handler.close_async = AsyncMock()
    adapter._handler = fake_handler
    adapter._app = MagicMock()

    await adapter.stop()
    fake_handler.close_async.assert_awaited_once()
    assert adapter._app is None
    assert adapter._handler is None


@pytest.mark.asyncio
async def test_stop_unstarted_is_noop() -> None:
    adapter = _build_adapter()
    await adapter.stop()  # MUST NOT raise


@pytest.mark.asyncio
async def test_stop_swallows_handler_close_error() -> None:
    """A failing close shouldn't prevent the adapter from clearing its
    state — daemon shutdown path keeps moving."""
    adapter = _build_adapter()
    fake_handler = MagicMock()
    fake_handler.close_async = AsyncMock(side_effect=RuntimeError("net err"))
    adapter._handler = fake_handler
    adapter._app = MagicMock()

    await adapter.stop()  # MUST NOT raise
    assert adapter._app is None
    assert adapter._handler is None


# ── lazy-import: missing slack-bolt ────────────────────────────────


@pytest.mark.asyncio
async def test_missing_slack_bolt_gives_clear_install_hint() -> None:
    """``import slack_bolt`` inside start() must fail with a RuntimeError
    that names the pip install command. Without the lazy-import, the
    daemon would crash at module import time for users who never
    enable Slack."""
    adapter = _build_adapter()

    # Block slack_bolt.async_app at import time.
    with patch.dict(sys.modules, {"slack_bolt.async_app": None}):
        with pytest.raises(RuntimeError) as exc_info:
            await adapter.start()

    msg = str(exc_info.value)
    # Operator gets a concrete command, not a stack trace.
    assert "pip install" in msg
    assert "slack" in msg.lower()
    assert adapter.last_start_error is not None
    assert "pip install" in adapter.last_start_error


def test_module_imports_without_slack_bolt() -> None:
    """Critical: ``from xmclaw.providers.channel.slack.adapter import
    SlackAdapter`` must succeed even when slack-bolt isn't installed.
    Otherwise the daemon's manifest discovery (which imports the
    package) crashes for every user who never enables Slack. Verified
    by reading the adapter source — module-level imports are limited
    to xmclaw + stdlib + the abstract base."""
    import xmclaw.providers.channel.slack.adapter as mod
    assert hasattr(mod, "SlackAdapter")
    # The adapter module did NOT pull slack_bolt into sys.modules by
    # side effect — verify by reading its source. The whole point of
    # the lazy import is that ``import xmclaw.providers.channel.slack.adapter``
    # stays clean.
    import inspect
    source = inspect.getsource(mod)
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("import slack_bolt", "from slack_bolt")):
            # It's a top-level import iff it has zero leading whitespace.
            assert line != stripped, (
                f"adapter.py has a TOP-LEVEL slack_bolt import: {line!r}; "
                "this would crash the daemon at import time when the "
                "extra is not installed"
            )
        if stripped.startswith(("import slack_sdk", "from slack_sdk")):
            assert line != stripped, (
                f"adapter.py has a TOP-LEVEL slack_sdk import: {line!r}"
            )


# ── manifest registration ────────────────────────────────────────


def test_slack_appears_in_registry_discover() -> None:
    """The slack package must register a ready-status manifest so
    ``discover()`` (used by the daemon's lifespan loop to wire enabled
    channels) sees it. Without this the user can set
    ``channels.slack.enabled=true`` and the daemon would log
    'channel.unknown id=slack' and silently skip."""
    from xmclaw.providers.channel.registry import discover
    manifests = discover()  # default include_scaffolds=False
    assert "slack" in manifests
    m = manifests["slack"]
    assert m.implementation_status == "ready"
    assert m.adapter_factory_path == (
        "xmclaw.providers.channel.slack.adapter:SlackAdapter"
    )
    # No public webhook needed — Socket Mode connects from our side.
    assert m.needs_tunnel is False


# ── helper-level coverage ─────────────────────────────────────────


def test_split_for_slack_under_cap() -> None:
    assert split_text("short", 4000) == ["short"]
    assert split_text("", 4000) == []


def test_split_for_slack_chunks_at_cap() -> None:
    text = "x" * 5000
    chunks = split_text(text, 3900)
    assert len(chunks) >= 2
    assert all(len(c) <= 3900 for c in chunks)
    # Reassembly returns the original.
    assert "".join(chunks) == text


def test_split_for_slack_prefers_newline_boundaries() -> None:
    para = "a" * 1500
    text = "\n\n".join([para, para, para, para])
    chunks = split_text(text, 3900)
    assert all(len(c) <= 3900 for c in chunks)


def test_coerce_str_set_handles_none_and_empty() -> None:
    assert _coerce_str_set(None, key="x") == set()
    assert _coerce_str_set([], key="x") == set()


def test_coerce_str_set_strips_whitespace() -> None:
    assert _coerce_str_set([" U123 ", "U456"], key="x") == {"U123", "U456"}


def test_coerce_str_set_drops_empty_strings() -> None:
    """Empty string entries are dropped — they'd never match any real
    Slack id and hide bugs in the config."""
    assert _coerce_str_set(["U1", "", "  "], key="x") == {"U1"}
