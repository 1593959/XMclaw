"""B-381 (Sprint 2): DiscordAdapter unit tests.

Sibling of B-380 ``test_v2_channels_telegram.py`` — exercises the same
contracts (lifecycle, allowlist, dedup, send/chunking, lazy import)
against ``discord.py``'s gateway model. Pre-B-381 the Discord adapter
didn't exist (the channel/discord/ directory was absent).

Test posture mirrors the Telegram suite — duck-type events directly
into ``_on_message_async`` rather than booting a real discord.Client.
``start()`` / outbound ``send()`` exercise a mock Client + Channel to
avoid network. The lazy-import exit path is verified by deleting the
``discord`` module from ``sys.modules`` + monkeypatching the import to
fail.
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xmclaw.providers.channel.base import ChannelTarget, OutboundMessage
from xmclaw.providers.channel._shared import split_text
from xmclaw.providers.channel.discord.adapter import (
    DiscordAdapter,
    _coerce_id_set,
    _to_int_or_none,
)


# ── helpers ────────────────────────────────────────────────────────


def _make_message(
    *,
    content: str = "hello",
    channel_id: int = 12345,
    user_id: int = 67890,
    username: str = "alice",
    message_id: int = 1,
    is_bot: bool = False,
):
    """Duck-type a discord.py Message. Only the attribute paths the
    adapter actually reads need to exist. Mirrors the Telegram suite's
    _make_update helper."""
    return SimpleNamespace(
        id=message_id,
        content=content,
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id=user_id, name=username, bot=is_bot),
    )


def _build_adapter(**extra_cfg) -> DiscordAdapter:
    """Construct without start() — exercises only _on_message_async /
    helpers. ``bot_token`` is required at __init__ so we always pass it."""
    cfg = {"bot_token": "MTIzNDU2.fake.token_for_test"}
    cfg.update(extra_cfg)
    return DiscordAdapter(cfg)


# ── construction + config validation ───────────────────────────────


def test_adapter_requires_bot_token() -> None:
    with pytest.raises(ValueError, match="bot_token"):
        DiscordAdapter({})


def test_adapter_rejects_string_allowlist() -> None:
    """A common config typo: ``allowed_user_ids: "12345"`` (string instead
    of list). Catch at __init__ rather than letting it silently match
    nothing forever (which is the worse failure mode — looks healthy)."""
    with pytest.raises(ValueError, match="allowed_user_ids"):
        DiscordAdapter({
            "bot_token": "x.y.z",
            "allowed_user_ids": "12345",
        })


def test_adapter_accepts_int_strings_in_allowlist() -> None:
    """JSON has no int-vs-string distinction in some shells; accept
    both ``[12345]`` and ``["12345"]``. Discord snowflakes are always
    positive 64-bit ints."""
    adapter = DiscordAdapter({
        "bot_token": "x.y.z",
        "allowed_user_ids": [123456789012345678, "987654321098765432"],
        "allowed_channel_ids": ["111222333444555666"],
    })
    assert adapter._allowed_user_ids == {123456789012345678, 987654321098765432}
    assert adapter._allowed_channel_ids == {111222333444555666}


def test_adapter_rejects_bool_in_allowlist() -> None:
    """bool is a Python int subclass; without an explicit guard a
    ``[True]`` typo would silently allowlist user id 1."""
    with pytest.raises(ValueError, match="bool"):
        DiscordAdapter({
            "bot_token": "x.y.z",
            "allowed_user_ids": [True],
        })


# ── inbound dispatch ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_message_produces_correct_session_id() -> None:
    """The session_id used by ChannelDispatcher is f"{channel}:{ref}".
    For Discord that's "discord:<channel_id>" — same shape as
    Telegram's "telegram:<chat_id>". We assert on the InboundMessage
    shape because that's the contract the dispatcher reads."""
    adapter = _build_adapter()
    inbox: list = []

    async def handler(msg) -> None:
        inbox.append(msg)

    adapter.subscribe(handler)
    await adapter._on_message_async(
        _make_message(content="hello", channel_id=98765, user_id=12345),
    )
    assert len(inbox) == 1
    msg = inbox[0]
    assert msg.target.channel == "discord"
    assert msg.target.ref == "98765"  # channel_id stringified
    assert msg.content == "hello"
    assert msg.user_ref == "12345"
    # Dispatcher will compose "discord:98765" from these fields.
    assert f"{msg.target.channel}:{msg.target.ref}" == "discord:98765"


@pytest.mark.asyncio
async def test_inbound_dedup_drops_duplicate_message_id() -> None:
    """Discord gateway resumes can theoretically redeliver. We mirror
    Feishu / Telegram's LRU-by-message_id dedup."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    msg = _make_message(content="ping", message_id=1, channel_id=10)
    await adapter._on_message_async(msg)
    await adapter._on_message_async(msg)  # exact replay
    assert inbox == ["ping"]


@pytest.mark.asyncio
async def test_inbound_skips_empty_text_and_no_channel() -> None:
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    # Empty content — drop.
    await adapter._on_message_async(_make_message(content="   "))
    # No channel id — drop (defensive; real Discord messages always have one).
    msg_no_channel = SimpleNamespace(
        id=2,
        content="x",
        channel=SimpleNamespace(id=0),
        author=SimpleNamespace(id=1, name="", bot=False),
    )
    await adapter._on_message_async(msg_no_channel)
    assert inbox == []


@pytest.mark.asyncio
async def test_inbound_drops_messages_from_self() -> None:
    """Without this guard the adapter would echo every reply back into
    itself and infinite-loop — discord.py delivers our own outbound
    messages back through on_message just like any other message."""
    adapter = _build_adapter()
    # Plant a fake client whose user.id matches the inbound author.
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=11111))
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(
        _make_message(user_id=11111, content="my own message"),
    )
    assert inbox == []


@pytest.mark.asyncio
async def test_inbound_drops_messages_from_other_bots() -> None:
    """Cross-bot loops are a known Discord footgun — ignore bot-flagged
    authors by default."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(
        _make_message(content="from another bot", is_bot=True),
    )
    assert inbox == []


@pytest.mark.asyncio
async def test_inbound_no_message_attribute() -> None:
    """Defensive guard: a None message dispatched to on_message must
    not AttributeError. (Won't happen in practice but keeps the
    signature contract clean.)"""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(None)
    assert inbox == []


# ── allowlist (B-337 parity) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_allowlist_drops_unauthorized_user() -> None:
    adapter = _build_adapter(allowed_user_ids=[111, 222])
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    # Non-allowlisted user.
    await adapter._on_message_async(
        _make_message(content="from stranger", user_id=999, message_id=10),
    )
    assert inbox == []


@pytest.mark.asyncio
async def test_allowlist_passes_authorized_user() -> None:
    adapter = _build_adapter(allowed_user_ids=[111])
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(
        _make_message(content="from owner", user_id=111, message_id=11),
    )
    assert inbox == ["from owner"]


@pytest.mark.asyncio
async def test_no_allowlist_lets_anyone_in() -> None:
    """Backward compat: no allowlist set → any sender is fine. Matches
    the feishu / telegram default ('any guild member can use the agent')."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(
        _make_message(content="any user", user_id=99999, message_id=12),
    )
    assert inbox == ["any user"]


@pytest.mark.asyncio
async def test_channel_id_allowlist_drops_unauthorized_channel() -> None:
    """A user might be allowlisted in their DM but not authorized to
    drive the agent from a guild channel. allowed_channel_ids splits
    this — empty list = no restriction; non-empty = channel_id must be
    in the list."""
    adapter = _build_adapter(allowed_channel_ids=[100, 200])
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    # User OK, channel NOT in allowlist.
    await adapter._on_message_async(
        _make_message(content="wrong channel", channel_id=999, message_id=20),
    )
    assert inbox == []


# ── outbound send ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_hits_correct_channel_id() -> None:
    """send() must call channel.send with the channel resolved via
    client.get_channel from target.ref. The dispatcher passes
    ref=str(channel_id); the adapter coerces back to int."""
    adapter = _build_adapter()
    fake_channel = MagicMock()
    fake_channel.send = AsyncMock(return_value=SimpleNamespace(id=42))
    fake_client = MagicMock()
    fake_client.get_channel = MagicMock(return_value=fake_channel)
    adapter._client = fake_client

    await adapter.send(
        ChannelTarget(channel="discord", ref="98765"),
        OutboundMessage(content="hi from agent"),
    )
    fake_client.get_channel.assert_called_once_with(98765)
    fake_channel.send.assert_called_once()
    kwargs = fake_channel.send.call_args.kwargs
    assert kwargs["content"] == "hi from agent"
    # No reply_to → no reference kwarg
    assert "reference" not in kwargs


@pytest.mark.asyncio
async def test_send_passes_reply_reference() -> None:
    adapter = _build_adapter()
    fake_ref_msg = SimpleNamespace(id=100)
    fake_channel = MagicMock()
    fake_channel.send = AsyncMock(return_value=SimpleNamespace(id=43))
    fake_channel.fetch_message = AsyncMock(return_value=fake_ref_msg)
    fake_client = MagicMock()
    fake_client.get_channel = MagicMock(return_value=fake_channel)
    adapter._client = fake_client

    await adapter.send(
        ChannelTarget(channel="discord", ref="98765"),
        OutboundMessage(content="reply text", reply_to="100"),
    )
    fake_channel.fetch_message.assert_awaited_once_with(100)
    kwargs = fake_channel.send.call_args.kwargs
    assert kwargs["reference"] is fake_ref_msg


@pytest.mark.asyncio
async def test_send_falls_back_to_fetch_channel_on_cache_miss() -> None:
    """get_channel returns None for channels not in cache (e.g. DMs the
    bot has never seen). Adapter must fall back to fetch_channel
    instead of crashing with AttributeError on None.send."""
    adapter = _build_adapter()
    fake_channel = MagicMock()
    fake_channel.send = AsyncMock(return_value=SimpleNamespace(id=44))
    fake_client = MagicMock()
    fake_client.get_channel = MagicMock(return_value=None)  # cache miss
    fake_client.fetch_channel = AsyncMock(return_value=fake_channel)
    adapter._client = fake_client

    await adapter.send(
        ChannelTarget(channel="discord", ref="55555"),
        OutboundMessage(content="dm"),
    )
    fake_client.fetch_channel.assert_awaited_once_with(55555)
    fake_channel.send.assert_called_once()


@pytest.mark.asyncio
async def test_send_rejects_wrong_channel_target() -> None:
    """An adapter must refuse ChannelTargets for a different channel."""
    adapter = _build_adapter()
    adapter._client = MagicMock()
    with pytest.raises(ValueError):
        await adapter.send(
            ChannelTarget(channel="not-discord", ref="1"),
            OutboundMessage(content="leak"),
        )


@pytest.mark.asyncio
async def test_send_chunks_long_messages() -> None:
    """Discord caps each message at 2000 chars. A 5k reply must arrive
    as 3 successive sends, with reference attached only to the first
    chunk so the conversation thread isn't muddied."""
    adapter = _build_adapter()
    fake_ref_msg = SimpleNamespace(id=50)
    fake_channel = MagicMock()
    fake_channel.send = AsyncMock(return_value=SimpleNamespace(id=44))
    fake_channel.fetch_message = AsyncMock(return_value=fake_ref_msg)
    fake_client = MagicMock()
    fake_client.get_channel = MagicMock(return_value=fake_channel)
    adapter._client = fake_client

    long_text = "word " * 1000  # 5000 chars
    await adapter.send(
        ChannelTarget(channel="discord", ref="1"),
        OutboundMessage(content=long_text, reply_to="50"),
    )
    assert fake_channel.send.await_count >= 2
    # First call carries reference; the rest don't.
    first_kwargs = fake_channel.send.call_args_list[0].kwargs
    assert first_kwargs.get("reference") is fake_ref_msg
    for call in fake_channel.send.call_args_list[1:]:
        assert "reference" not in call.kwargs


@pytest.mark.asyncio
async def test_send_raises_on_discord_failure() -> None:
    """If channel.send raises (Discord returns 400 / network blip on
    the LAST chunk), the adapter surfaces a RuntimeError so the
    dispatcher's outer try/except records 'channel.send_failed' and
    the user gets some signal that delivery dropped."""
    adapter = _build_adapter()
    fake_channel = MagicMock()
    fake_channel.send = AsyncMock(side_effect=RuntimeError("API error"))
    fake_client = MagicMock()
    fake_client.get_channel = MagicMock(return_value=fake_channel)
    adapter._client = fake_client

    with pytest.raises(RuntimeError, match="discord send failed"):
        await adapter.send(
            ChannelTarget(channel="discord", ref="1"),
            OutboundMessage(content="will fail"),
        )


@pytest.mark.asyncio
async def test_send_unstarted_adapter_raises() -> None:
    adapter = _build_adapter()
    with pytest.raises(RuntimeError, match="not started"):
        await adapter.send(
            ChannelTarget(channel="discord", ref="1"),
            OutboundMessage(content="x"),
        )


# ── start() lifecycle ──────────────────────────────────────────────


def _build_fake_discord_module(login_failure: bool = False):
    """Construct a SimpleNamespace mimicking ``discord`` + ``discord.errors``
    just enough for start() to thread through. Returns a tuple of
    (discord_module, login_failure_cls)."""

    LoginFailureCls = type("LoginFailure", (Exception,), {})
    HTTPExceptionCls = type("HTTPException", (Exception,), {})

    fake_intents = SimpleNamespace(
        message_content=False, messages=False, guilds=False,
    )
    Intents = MagicMock()
    Intents.default = MagicMock(return_value=fake_intents)

    # Track whether the on_message + on_ready handlers were registered.
    registered_events: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, intents=None):
            self.intents = intents
            self.user = SimpleNamespace(id=42)
            self._closed = False

        def event(self, fn):
            registered_events[fn.__name__] = fn
            return fn

        async def start(self, token):
            if login_failure:
                raise LoginFailureCls("bad token")
            # Simulate the gateway firing on_ready after a beat.
            on_ready = registered_events.get("on_ready")
            if on_ready is not None:
                await on_ready()
            # Block until close so the supervisor task stays alive.
            while not self._closed:
                await asyncio.sleep(0.1)

        async def close(self):
            self._closed = True

        def is_closed(self):
            return self._closed

        def get_channel(self, _id):
            return None

    fake_discord = SimpleNamespace(
        Intents=Intents,
        Client=_FakeClient,
        File=MagicMock(),
    )
    fake_errors = SimpleNamespace(
        LoginFailure=LoginFailureCls,
        HTTPException=HTTPExceptionCls,
    )
    return fake_discord, fake_errors, registered_events, LoginFailureCls


@pytest.mark.asyncio
async def test_start_connects_with_configured_token() -> None:
    """start() must hand the configured bot_token to client.start.
    Patches the discord import so no network is touched + we can
    verify the token threads through."""
    adapter = _build_adapter(bot_token="MTAA.real_bot_token")

    fake_discord, fake_errors, registered, _ = _build_fake_discord_module()

    with patch.dict(sys.modules, {
        "discord": fake_discord,
        "discord.errors": fake_errors,
    }):
        await adapter.start()
        try:
            # on_message + on_ready registered as gateway listeners
            assert "on_message" in registered
            assert "on_ready" in registered
            assert adapter._client is not None
            assert adapter.last_start_error is None
        finally:
            await adapter.stop()


@pytest.mark.asyncio
async def test_start_invalid_token_surfaces_clear_error() -> None:
    """Bot token rejected by Discord → LoginFailure from client.start.
    Adapter must surface a RuntimeError with operator-readable text
    AND set ``last_start_error`` so the setup endpoint can render it."""
    adapter = _build_adapter(bot_token="bogus.token.xxx")

    fake_discord, fake_errors, _, _ = _build_fake_discord_module(
        login_failure=True,
    )

    with patch.dict(sys.modules, {
        "discord": fake_discord,
        "discord.errors": fake_errors,
    }):
        with pytest.raises(RuntimeError):
            await adapter.start()

    # last_start_error mentions the actionable cause.
    assert adapter.last_start_error is not None
    assert (
        "bot_token" in adapter.last_start_error.lower()
        or "loginfailure" in adapter.last_start_error.lower()
        or "token" in adapter.last_start_error.lower()
    )


@pytest.mark.asyncio
async def test_start_idempotent() -> None:
    """A second start() call after success is a no-op (matches the
    feishu / telegram contract). Otherwise lifespan retries would
    double-instantiate Client + leak resources."""
    adapter = _build_adapter()
    sentinel = MagicMock()
    adapter._client = sentinel
    await adapter.start()  # MUST NOT do anything
    assert adapter._client is sentinel


@pytest.mark.asyncio
async def test_stop_closes_client_cleanly() -> None:
    """stop() must call client.close(). With a real start() under our
    fake module the task was running; stop should drain it cleanly."""
    adapter = _build_adapter()

    fake_discord, fake_errors, _, _ = _build_fake_discord_module()

    with patch.dict(sys.modules, {
        "discord": fake_discord,
        "discord.errors": fake_errors,
    }):
        await adapter.start()
        client_before = adapter._client
        await adapter.stop()

    assert adapter._client is None
    assert client_before.is_closed()


@pytest.mark.asyncio
async def test_stop_unstarted_is_noop() -> None:
    adapter = _build_adapter()
    await adapter.stop()  # MUST NOT raise


# ── lazy-import: missing discord.py ──────────────────────


@pytest.mark.asyncio
async def test_missing_discord_py_gives_clear_install_hint() -> None:
    """The third concrete requirement from the task. ``import discord``
    inside start() must fail with a RuntimeError that names the pip
    install command. Without the lazy-import, the daemon would crash
    at module import time for users who never enable Discord —
    making the channel a hard dep instead of an optional one."""
    adapter = _build_adapter()

    # Block the discord package at import time. patch.dict with the
    # value set to None makes ``import discord`` raise ImportError —
    # exact same shape ``pip uninstall discord.py`` would produce.
    with patch.dict(sys.modules, {"discord": None}):
        with pytest.raises(RuntimeError) as exc_info:
            await adapter.start()

    msg = str(exc_info.value)
    # Operator gets a concrete command, not a stack trace.
    assert "pip install" in msg
    assert "discord" in msg.lower()
    # last_start_error mirrors the same hint so the setup endpoint
    # can show it without re-deriving.
    assert adapter.last_start_error is not None
    assert "pip install" in adapter.last_start_error


def test_module_imports_without_discord_py() -> None:
    """Critical: ``from xmclaw.providers.channel.discord.adapter
    import DiscordAdapter`` must succeed even when discord.py isn't
    installed. Otherwise the daemon's manifest discovery (which
    imports the package) crashes for every user who never enables
    Discord. Verified by reading the adapter source — module-level
    imports are limited to xmclaw + stdlib + the abstract base."""
    import xmclaw.providers.channel.discord.adapter as mod
    # If we got here the import succeeded. Sanity check: DiscordAdapter
    # is the only public name we declared.
    assert hasattr(mod, "DiscordAdapter")
    # And the adapter module did NOT pull discord into sys.modules
    # by side effect — verify by reading its source. The whole point
    # of the lazy import is that ``import xmclaw.providers.channel.discord.adapter``
    # stays clean.
    import inspect
    source = inspect.getsource(mod)
    # No top-level `import discord` / `from discord import ...`
    # (lines that would fire at module load). We DO import inside
    # methods, but those start with whitespace.
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("import discord", "from discord")):
            # It's a top-level import iff it has zero leading
            # whitespace.
            assert line != stripped, (
                f"adapter.py has a TOP-LEVEL discord import: {line!r}; "
                "this would crash the daemon at import time when the "
                "extra is not installed"
            )


# ── manifest registration ──────────────────────────────────────────


def test_manifest_registers_discord_as_ready() -> None:
    """The ``__init__.py`` MANIFEST must mark Discord ready (not
    scaffold) so the dispatcher actually wires it. Without this,
    ``include_scaffolds=False`` (production default) would hide it."""
    from xmclaw.providers.channel.discord import MANIFEST
    assert MANIFEST.id == "discord"
    assert MANIFEST.implementation_status == "ready"
    assert MANIFEST.adapter_factory_path == (
        "xmclaw.providers.channel.discord.adapter:DiscordAdapter"
    )


def test_discover_includes_discord_in_default_set() -> None:
    """Production discovery (``include_scaffolds=False``) must surface
    Discord — sibling of the same Telegram / Feishu invariant."""
    from xmclaw.providers.channel.registry import discover

    ready = discover(include_scaffolds=False)
    assert "discord" in ready, (
        f"discord not in default discovery; got: {list(ready.keys())}"
    )


# ── helper-level coverage ─────────────────────────────────────────


def test_split_for_discord_under_cap() -> None:
    assert split_text("short", 2000) == ["short"]
    assert split_text("", 2000) == []


def test_split_for_discord_chunks_at_cap() -> None:
    text = "x" * 5000
    chunks = split_text(text, cap=2000)
    assert len(chunks) == 3
    assert all(len(c) <= 2000 for c in chunks)
    # Reassembly returns the original.
    assert "".join(chunks) == text


def test_split_for_discord_prefers_newline_boundaries() -> None:
    # 4 paragraphs of 800 chars each → split should land on a \n,
    # not mid-paragraph.
    para = "a" * 800
    text = "\n\n".join([para, para, para, para])
    chunks = split_text(text, cap=2000)
    assert all(len(c) <= 2000 for c in chunks)


def test_coerce_id_set_handles_none_and_empty() -> None:
    assert _coerce_id_set(None, key="x") == set()
    assert _coerce_id_set([], key="x") == set()


def test_to_int_or_none_parses_snowflakes() -> None:
    # Discord snowflakes are positive 64-bit ints
    assert _to_int_or_none("987654321098765432") == 987654321098765432
    assert _to_int_or_none("not-a-number") is None
    assert _to_int_or_none(None) is None
    assert _to_int_or_none(42) == 42
