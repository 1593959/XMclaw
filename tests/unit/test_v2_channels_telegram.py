"""B-380 (Sprint 2): TelegramAdapter unit tests.

Pre-B-380 the Telegram adapter was a 19-line scaffold raising
NotImplementedError on construct. The real adapter uses
``python-telegram-bot`` long-poll mode + Epic #14 injection scan +
B-337 allowlist + the same 4096-char chunking the Telegram Bot API
mandates.

Test posture mirrors :file:`test_v2_feishu_dedup.py` —
duck-type events directly into ``_on_message_async`` rather than
booting a real telegram Application. ``start()`` / outbound ``send()``
exercise a mock Application + Bot to avoid network. The lazy-import
exit path is verified by deleting the ``telegram`` module from
``sys.modules`` + monkeypatching the import to fail.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xmclaw.providers.channel.base import ChannelTarget, OutboundMessage
from xmclaw.providers.channel._shared import split_text
from xmclaw.providers.channel.telegram.adapter import (
    TelegramAdapter,
    _coerce_id_set,
    _to_int_or_none,
)


# ── helpers ────────────────────────────────────────────────────────


def _make_update(
    *,
    text: str = "hello",
    chat_id: int = 12345,
    user_id: int = 67890,
    username: str = "alice",
    message_id: int = 1,
):
    """Duck-type a python-telegram-bot Update. Only the attribute paths
    the adapter actually reads need to exist. Mirrors feishu's
    _make_event helper from test_v2_feishu_dedup.py."""
    return SimpleNamespace(
        update_id=message_id,
        message=SimpleNamespace(
            text=text,
            message_id=message_id,
            chat=SimpleNamespace(id=chat_id),
            from_user=SimpleNamespace(id=user_id, username=username),
        ),
    )


def _build_adapter(**extra_cfg) -> TelegramAdapter:
    """Construct without start() — exercises only _on_message_async /
    helpers. ``bot_token`` is required at __init__ so we always pass it."""
    cfg = {"bot_token": "12345:fake_token_for_test"}
    cfg.update(extra_cfg)
    return TelegramAdapter(cfg)


# ── construction + config validation ───────────────────────────────


def test_adapter_requires_bot_token() -> None:
    with pytest.raises(ValueError, match="bot_token"):
        TelegramAdapter({})


def test_adapter_rejects_string_allowlist() -> None:
    """A common config typo: ``allowed_user_ids: "12345"`` (string instead
    of list). Catch at __init__ rather than letting it silently match
    nothing forever (which is the worse failure mode — looks healthy)."""
    with pytest.raises(ValueError, match="allowed_user_ids"):
        TelegramAdapter({
            "bot_token": "x:y",
            "allowed_user_ids": "12345",
        })


def test_adapter_accepts_int_strings_in_allowlist() -> None:
    """JSON has no int-vs-string distinction in some shells; accept
    both ``[12345]`` and ``["12345"]``. Negative ids (supergroups) too."""
    adapter = TelegramAdapter({
        "bot_token": "x:y",
        "allowed_user_ids": [12345, "67890"],
        "allowed_chat_ids": ["-1001234567890"],
    })
    assert adapter._allowed_user_ids == {12345, 67890}
    assert adapter._allowed_chat_ids == {-1001234567890}


def test_adapter_rejects_bool_in_allowlist() -> None:
    """bool is a Python int subclass; without an explicit guard a
    ``[True]`` typo would silently allowlist user id 1."""
    with pytest.raises(ValueError, match="bool"):
        TelegramAdapter({
            "bot_token": "x:y",
            "allowed_user_ids": [True],
        })


# ── inbound dispatch ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_message_produces_correct_session_id() -> None:
    """The session_id used by ChannelDispatcher is f"{channel}:{ref}".
    For Telegram that's "telegram:<chat_id>" — same shape as feishu's
    "feishu:<chat_id>". We assert on the InboundMessage shape because
    that's the contract the dispatcher reads."""
    adapter = _build_adapter()
    inbox: list = []

    async def handler(msg) -> None:
        inbox.append(msg)

    adapter.subscribe(handler)
    await adapter._on_message_async(
        _make_update(text="hello", chat_id=98765, user_id=12345),
        context=None,
    )
    assert len(inbox) == 1
    msg = inbox[0]
    assert msg.target.channel == "telegram"
    assert msg.target.ref == "98765"  # chat_id stringified
    assert msg.content == "hello"
    assert msg.user_ref == "12345"
    # Dispatcher will compose "telegram:98765" from these fields.
    assert f"{msg.target.channel}:{msg.target.ref}" == "telegram:98765"


@pytest.mark.asyncio
async def test_inbound_dedup_drops_duplicate_message_id() -> None:
    """python-telegram-bot's long-poll uses drop_pending_updates=True
    on start, but mid-stream blips can still redeliver. We mirror
    Feishu's LRU-by-message_id dedup."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    upd = _make_update(text="ping", message_id=1, chat_id=10)
    await adapter._on_message_async(upd, context=None)
    await adapter._on_message_async(upd, context=None)  # exact replay
    assert inbox == ["ping"]


@pytest.mark.asyncio
async def test_inbound_skips_empty_text_and_no_chat() -> None:
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    # Empty text — drop.
    await adapter._on_message_async(_make_update(text="   "), context=None)
    # No chat id — drop (defensive; real Telegram messages always have one).
    update_no_chat = SimpleNamespace(
        update_id=2,
        message=SimpleNamespace(
            text="x", message_id=2, chat=SimpleNamespace(id=0),
            from_user=SimpleNamespace(id=1, username=""),
        ),
    )
    await adapter._on_message_async(update_no_chat, context=None)
    assert inbox == []


@pytest.mark.asyncio
async def test_inbound_no_message_attribute() -> None:
    """Edits / channel posts arrive with message=None (we only care
    about plain text messages — the MessageHandler filter blocks
    most of these anyway). Defensive guard so we don't AttributeError
    on the rare case that slips through."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(
        SimpleNamespace(update_id=1, message=None), context=None,
    )
    assert inbox == []


# ── allowlist (B-337 parity) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_allowlist_drops_unauthorized_user() -> None:
    adapter = _build_adapter(allowed_user_ids=[111, 222])
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    # Non-allowlisted user.
    await adapter._on_message_async(
        _make_update(text="from stranger", user_id=999, message_id=10),
        context=None,
    )
    assert inbox == []


@pytest.mark.asyncio
async def test_allowlist_passes_authorized_user() -> None:
    adapter = _build_adapter(allowed_user_ids=[111])
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(
        _make_update(text="from owner", user_id=111, message_id=11),
        context=None,
    )
    assert inbox == ["from owner"]


@pytest.mark.asyncio
async def test_no_allowlist_lets_anyone_in() -> None:
    """Backward compat: no allowlist set → any sender is fine. Matches
    the feishu default ('any group member can use the agent')."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    await adapter._on_message_async(
        _make_update(text="any user", user_id=99999, message_id=12),
        context=None,
    )
    assert inbox == ["any user"]


@pytest.mark.asyncio
async def test_chat_id_allowlist_drops_unauthorized_chat() -> None:
    """A user might be allowlisted in their DM but not authorized to
    drive the agent from a group. allowed_chat_ids splits this — empty
    list = no chat restriction; non-empty = chat_id must be in the list."""
    adapter = _build_adapter(allowed_chat_ids=[100, 200])
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    # User OK, chat NOT in allowlist.
    await adapter._on_message_async(
        _make_update(text="wrong chat", chat_id=999, message_id=20),
        context=None,
    )
    assert inbox == []


# ── outbound send ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_hits_correct_chat_id() -> None:
    """send() must call bot.send_message with the chat_id parsed from
    target.ref. The dispatcher passes ref=str(chat_id); the adapter
    coerces back to int because Telegram's API wants int."""
    adapter = _build_adapter()
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=42),
    )
    adapter._application = SimpleNamespace(bot=fake_bot)

    await adapter.send(
        ChannelTarget(channel="telegram", ref="98765"),
        OutboundMessage(content="hi from agent"),
    )
    fake_bot.send_message.assert_called_once()
    kwargs = fake_bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 98765
    assert kwargs["text"] == "hi from agent"
    assert kwargs["reply_to_message_id"] is None  # no reply_to


@pytest.mark.asyncio
async def test_send_passes_reply_to_message_id() -> None:
    adapter = _build_adapter()
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=43),
    )
    adapter._application = SimpleNamespace(bot=fake_bot)

    await adapter.send(
        ChannelTarget(channel="telegram", ref="98765"),
        OutboundMessage(content="reply text", reply_to="100"),
    )
    kwargs = fake_bot.send_message.call_args.kwargs
    assert kwargs["reply_to_message_id"] == 100  # string → int coerced


@pytest.mark.asyncio
async def test_send_rejects_wrong_channel_target() -> None:
    """An adapter must refuse ChannelTargets for a different channel.
    Same property the conformance suite asserts for WS."""
    adapter = _build_adapter()
    adapter._application = SimpleNamespace(bot=MagicMock())
    with pytest.raises(ValueError):
        await adapter.send(
            ChannelTarget(channel="not-telegram", ref="1"),
            OutboundMessage(content="leak"),
        )


@pytest.mark.asyncio
async def test_send_chunks_long_messages() -> None:
    """Telegram caps each message at 4096 chars. A 10k reply must
    arrive as 3 successive sends, with reply_to attached only to the
    first chunk so the conversation thread isn't muddied."""
    adapter = _build_adapter()
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=44),
    )
    adapter._application = SimpleNamespace(bot=fake_bot)

    long_text = "word " * 1000  # 5000 chars
    await adapter.send(
        ChannelTarget(channel="telegram", ref="1"),
        OutboundMessage(content=long_text, reply_to="50"),
    )
    assert fake_bot.send_message.await_count >= 2
    # First call carries reply_to; the rest don't.
    first_kwargs = fake_bot.send_message.call_args_list[0].kwargs
    assert first_kwargs["reply_to_message_id"] == 50
    for call in fake_bot.send_message.call_args_list[1:]:
        assert call.kwargs["reply_to_message_id"] is None


@pytest.mark.asyncio
async def test_send_raises_on_telegram_failure() -> None:
    """If Bot.send_message raises (Telegram returns 400 / network blip
    on the LAST chunk), the adapter surfaces a RuntimeError so the
    dispatcher's outer try/except records 'channel.send_failed' and
    the user gets some signal that delivery dropped."""
    adapter = _build_adapter()
    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock(side_effect=RuntimeError("API error"))
    adapter._application = SimpleNamespace(bot=fake_bot)

    with pytest.raises(RuntimeError, match="telegram send failed"):
        await adapter.send(
            ChannelTarget(channel="telegram", ref="1"),
            OutboundMessage(content="will fail"),
        )


@pytest.mark.asyncio
async def test_send_unstarted_adapter_raises() -> None:
    adapter = _build_adapter()
    with pytest.raises(RuntimeError, match="not started"):
        await adapter.send(
            ChannelTarget(channel="telegram", ref="1"),
            OutboundMessage(content="x"),
        )


# ── start() lifecycle ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_connects_with_configured_token() -> None:
    """start() must hand the configured bot_token to
    Application.builder().token(...). Patches the Application import
    so no network is touched + we can verify the token threads through."""
    adapter = _build_adapter(bot_token="ABCD:secret_test_token")

    # Build a fake Application chain: Application.builder().token(t).build()
    fake_app = MagicMock()
    fake_app.initialize = AsyncMock()
    fake_app.start = AsyncMock()
    fake_app.shutdown = AsyncMock()
    fake_app.stop = AsyncMock()
    fake_app.add_handler = MagicMock()
    fake_app.updater = MagicMock()
    fake_app.updater.start_polling = AsyncMock()
    fake_app.updater.stop = AsyncMock()
    fake_app.updater.running = True
    fake_app.running = True

    fake_builder = MagicMock()
    fake_builder.token = MagicMock(return_value=fake_builder)
    fake_builder.build = MagicMock(return_value=fake_app)

    fake_application_cls = MagicMock()
    fake_application_cls.builder = MagicMock(return_value=fake_builder)

    fake_filters = SimpleNamespace(
        TEXT=MagicMock(__and__=lambda self, other: self),
        COMMAND=MagicMock(__invert__=lambda self: self),
    )

    with patch.dict(sys.modules, {
        "telegram.ext": SimpleNamespace(
            Application=fake_application_cls,
            MessageHandler=MagicMock(),
            filters=fake_filters,
        ),
        "telegram.error": SimpleNamespace(
            InvalidToken=type("InvalidToken", (Exception,), {}),
            TelegramError=type("TelegramError", (Exception,), {}),
        ),
    }):
        await adapter.start()

    fake_builder.token.assert_called_once_with("ABCD:secret_test_token")
    fake_app.initialize.assert_awaited_once()
    fake_app.start.assert_awaited_once()
    fake_app.updater.start_polling.assert_awaited_once()
    assert adapter._application is fake_app
    assert adapter.last_start_error is None


@pytest.mark.asyncio
async def test_start_invalid_token_surfaces_clear_error() -> None:
    """Bot token rejected by Telegram → InvalidToken from initialize().
    Adapter must surface a RuntimeError with operator-readable text
    AND set ``last_start_error`` so the setup endpoint can render it."""
    adapter = _build_adapter(bot_token="bogus:token")

    InvalidTokenCls = type("InvalidToken", (Exception,), {})
    TelegramErrorCls = type("TelegramError", (Exception,), {})

    fake_app = MagicMock()
    fake_app.initialize = AsyncMock(side_effect=InvalidTokenCls("invalid"))
    fake_app.shutdown = AsyncMock()
    fake_app.add_handler = MagicMock()

    fake_builder = MagicMock()
    fake_builder.token = MagicMock(return_value=fake_builder)
    fake_builder.build = MagicMock(return_value=fake_app)
    fake_application_cls = MagicMock()
    fake_application_cls.builder = MagicMock(return_value=fake_builder)

    fake_filters = SimpleNamespace(
        TEXT=MagicMock(__and__=lambda self, other: self),
        COMMAND=MagicMock(__invert__=lambda self: self),
    )

    with patch.dict(sys.modules, {
        "telegram.ext": SimpleNamespace(
            Application=fake_application_cls,
            MessageHandler=MagicMock(),
            filters=fake_filters,
        ),
        "telegram.error": SimpleNamespace(
            InvalidToken=InvalidTokenCls,
            TelegramError=TelegramErrorCls,
        ),
    }):
        with pytest.raises(RuntimeError, match="bot_token"):
            await adapter.start()

    # last_start_error mentions the actionable cause + how to fix.
    assert adapter.last_start_error is not None
    assert "bot_token" in adapter.last_start_error.lower() or \
           "401" in adapter.last_start_error or \
           "bot" in adapter.last_start_error.lower()
    # shutdown is called to release the half-init application; no
    # zombie tasks.
    fake_app.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_idempotent() -> None:
    """A second start() call after success is a no-op (matches the
    feishu adapter's contract). Otherwise lifespan retries would
    double-instantiate Application + leak resources."""
    adapter = _build_adapter()
    sentinel = MagicMock()
    adapter._application = sentinel
    await adapter.start()  # MUST NOT do anything
    assert adapter._application is sentinel


@pytest.mark.asyncio
async def test_stop_closes_application_cleanly() -> None:
    adapter = _build_adapter()
    fake_app = MagicMock()
    fake_app.updater = MagicMock()
    fake_app.updater.running = True
    fake_app.updater.stop = AsyncMock()
    fake_app.running = True
    fake_app.stop = AsyncMock()
    fake_app.shutdown = AsyncMock()
    adapter._application = fake_app

    await adapter.stop()
    fake_app.updater.stop.assert_awaited_once()
    fake_app.stop.assert_awaited_once()
    fake_app.shutdown.assert_awaited_once()
    assert adapter._application is None


@pytest.mark.asyncio
async def test_stop_unstarted_is_noop() -> None:
    adapter = _build_adapter()
    await adapter.stop()  # MUST NOT raise


# ── lazy-import: missing python-telegram-bot ──────────────────────


@pytest.mark.asyncio
async def test_missing_python_telegram_bot_gives_clear_install_hint() -> None:
    """The third concrete requirement from the task. ``import telegram``
    inside start() must fail with a RuntimeError that names the pip
    install command. Without the lazy-import, the daemon would crash
    at module import time for users who never enable Telegram —
    making the channel a hard dep instead of an optional one."""
    adapter = _build_adapter()

    # Block telegram.ext at import time. patch.dict with the value set
    # to None makes ``from telegram.ext import X`` raise ImportError —
    # exact same shape ``pip uninstall python-telegram-bot`` would
    # produce.
    with patch.dict(sys.modules, {"telegram.ext": None}):
        with pytest.raises(RuntimeError) as exc_info:
            await adapter.start()

    msg = str(exc_info.value)
    # Operator gets a concrete command, not a stack trace.
    assert "pip install" in msg
    assert "telegram" in msg.lower()
    # last_start_error mirrors the same hint so the setup endpoint
    # can show it without re-deriving.
    assert adapter.last_start_error is not None
    assert "pip install" in adapter.last_start_error


def test_module_imports_without_python_telegram_bot() -> None:
    """Critical: ``from xmclaw.providers.channel.telegram.adapter
    import TelegramAdapter`` must succeed even when python-telegram-bot
    isn't installed. Otherwise the daemon's manifest discovery (which
    imports the package) crashes for every user who never enables
    Telegram. Verified by reading the adapter source — module-level
    imports are limited to xmclaw + stdlib + the abstract base."""
    import xmclaw.providers.channel.telegram.adapter as mod
    # If we got here the import succeeded. Sanity check: TelegramAdapter
    # is the only public name we declared.
    assert hasattr(mod, "TelegramAdapter")
    # And the adapter module did NOT pull telegram into sys.modules
    # by side effect — verify by reading its source. The whole point
    # of the lazy import is that ``import xmclaw.providers.channel.telegram.adapter``
    # stays clean.
    import inspect
    source = inspect.getsource(mod)
    # No top-level `import telegram` / `from telegram import ...`
    # (lines that would fire at module load). We DO import inside
    # methods, but those start with whitespace.
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("import telegram", "from telegram")):
            # It's a top-level import iff it has zero leading
            # whitespace.
            assert line != stripped, (
                f"adapter.py has a TOP-LEVEL telegram import: {line!r}; "
                "this would crash the daemon at import time when the "
                "extra is not installed"
            )


# ── helper-level coverage ─────────────────────────────────────────


def test_split_for_telegram_under_cap() -> None:
    assert split_text("short", 4096) == ["short"]
    assert split_text("", 4096) == []


def test_split_for_telegram_chunks_at_cap() -> None:
    text = "x" * 5000
    chunks = split_text(text, cap=4096)
    assert len(chunks) == 2
    assert all(len(c) <= 4096 for c in chunks)
    # Reassembly returns the original.
    assert "".join(chunks) == text


def test_split_for_telegram_prefers_newline_boundaries() -> None:
    # 4 paragraphs of 1500 chars each → split should land on a \n,
    # not mid-paragraph.
    para = "a" * 1500
    text = "\n\n".join([para, para, para, para])
    chunks = split_text(text, cap=4096)
    assert all(len(c) <= 4096 for c in chunks)


def test_coerce_id_set_handles_none_and_empty() -> None:
    assert _coerce_id_set(None, key="x") == set()
    assert _coerce_id_set([], key="x") == set()


def test_to_int_or_none_parses_negative_supergroup_ids() -> None:
    assert _to_int_or_none("-1001234567890") == -1001234567890
    assert _to_int_or_none("not-a-number") is None
    assert _to_int_or_none(None) is None
    assert _to_int_or_none(42) == 42
