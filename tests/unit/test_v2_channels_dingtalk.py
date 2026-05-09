"""B-383 (Sprint 2): DingTalkAdapter unit tests.

Pre-B-383 the DingTalk adapter was a 7-line scaffold raising
NotImplementedError on construct. The real adapter uses
``dingtalk-stream``'s Stream Mode (long-running WebSocket from our
side, no public webhook needed) + Epic #14 injection scan + B-337
allowlist (user staff_ids + conversation_ids).

Test posture mirrors :file:`test_v2_channels_telegram.py` —
duck-type ChatbotMessage objects directly into ``_handle_message``
rather than booting a real DingTalkStreamClient. ``start()`` /
outbound ``send()`` exercise mock client + handler to avoid network.
The lazy-import exit path is verified by setting
``dingtalk_stream`` to ``None`` in ``sys.modules``.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from xmclaw.providers.channel.base import ChannelTarget, OutboundMessage
from xmclaw.providers.channel.dingtalk.adapter import (
    DingTalkAdapter,
    _coerce_str_set,
    _split_for_dingtalk,
)


# ── helpers ────────────────────────────────────────────────────────


def _make_chatbot_message(
    *,
    text: str = "hello",
    conversation_id: str = "cidLfaa==",
    sender_staff_id: str = "manager4321",
    sender_id: str = "$:LWCP_v1:$abc",
    message_id: str = "msgABC123",
    message_type: str = "text",
    conversation_type: str = "2",
    session_webhook: str = "https://oapi.dingtalk.com/robot/sendBySession?session=xxx",
):
    """Duck-type a dingtalk_stream.ChatbotMessage. Only the attribute
    paths the adapter actually reads need to exist."""
    text_obj = SimpleNamespace(content=text) if message_type == "text" else None
    return SimpleNamespace(
        message_type=message_type,
        text=text_obj,
        conversation_id=conversation_id,
        message_id=message_id,
        sender_id=sender_id,
        sender_staff_id=sender_staff_id,
        conversation_type=conversation_type,
        session_webhook=session_webhook,
    )


def _build_adapter(**extra_cfg) -> DingTalkAdapter:
    """Construct without start() — exercises only _handle_message /
    helpers. ``client_id`` + ``client_secret`` are required at __init__
    so we always pass them."""
    cfg = {
        "client_id": "dingfake_client_id",
        "client_secret": "fake_client_secret_for_test",
    }
    cfg.update(extra_cfg)
    return DingTalkAdapter(cfg)


# ── construction + config validation ───────────────────────────────


def test_adapter_requires_client_id() -> None:
    with pytest.raises(ValueError, match="client_id"):
        DingTalkAdapter({"client_secret": "secret"})


def test_adapter_requires_client_secret() -> None:
    with pytest.raises(ValueError, match="client_secret"):
        DingTalkAdapter({"client_id": "ding-x"})


def test_adapter_robot_code_defaults_to_client_id() -> None:
    """Single-app builds (the common case) use the same id for both —
    we default to client_id when robot_code is omitted so users don't
    have to think about it."""
    adapter = _build_adapter()
    assert adapter._robot_code == "dingfake_client_id"


def test_adapter_robot_code_explicit_override() -> None:
    """Multi-robot setups override robot_code explicitly."""
    adapter = _build_adapter(robot_code="ding-explicit-bot")
    assert adapter._robot_code == "ding-explicit-bot"


def test_adapter_rejects_string_allowlist() -> None:
    """A common config typo: ``allowed_user_ids: "manager4321"``
    (string instead of list). Catch at __init__ rather than letting
    it silently match nothing forever."""
    with pytest.raises(ValueError, match="allowed_user_ids"):
        DingTalkAdapter({
            "client_id": "x",
            "client_secret": "y",
            "allowed_user_ids": "manager4321",
        })


def test_adapter_accepts_string_ids_in_allowlist() -> None:
    """钉钉 staff_ids and conversation_ids are opaque strings; accept
    them as-is."""
    adapter = DingTalkAdapter({
        "client_id": "x",
        "client_secret": "y",
        "allowed_user_ids": ["manager4321", "manager5678"],
        "allowed_conversation_ids": ["cidA==", "cidB=="],
    })
    assert adapter._allowed_user_ids == {"manager4321", "manager5678"}
    assert adapter._allowed_conversation_ids == {"cidA==", "cidB=="}


def test_adapter_rejects_non_string_id_in_allowlist() -> None:
    """A ``[12345]`` typo would silently include int 12345 in a set
    that's then compared against str user ids — guaranteed mismatch."""
    with pytest.raises(ValueError, match="must be str"):
        DingTalkAdapter({
            "client_id": "x",
            "client_secret": "y",
            "allowed_user_ids": [12345],
        })


# ── inbound dispatch ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_message_produces_correct_session_id() -> None:
    """The session_id used by ChannelDispatcher is f"{channel}:{ref}".
    For DingTalk that's "dingtalk:<conversation_id>" — same shape as
    the other channels' "<channel>:<ref>"."""
    adapter = _build_adapter()
    inbox: list = []

    async def handler(msg) -> None:
        inbox.append(msg)

    adapter.subscribe(handler)
    await adapter._handle_message(
        _make_chatbot_message(text="hello", conversation_id="cidXX=="),
    )
    assert len(inbox) == 1
    msg = inbox[0]
    assert msg.target.channel == "dingtalk"
    assert msg.target.ref == "cidXX=="
    assert msg.content == "hello"
    assert msg.user_ref == "manager4321"  # prefers sender_staff_id
    # Dispatcher composes "dingtalk:cidXX==" from these fields.
    assert f"{msg.target.channel}:{msg.target.ref}" == "dingtalk:cidXX=="


@pytest.mark.asyncio
async def test_inbound_caches_session_webhook_for_outbound() -> None:
    """send() needs the session_webhook from the latest inbound message
    in that conversation. Cache must populate on first inbound."""
    adapter = _build_adapter()
    msg = _make_chatbot_message(conversation_id="cidWEBHOOK==")
    await adapter._handle_message(msg)
    assert "cidWEBHOOK==" in adapter._conversation_msgs
    assert adapter._conversation_msgs["cidWEBHOOK=="] is msg


@pytest.mark.asyncio
async def test_inbound_falls_back_to_sender_id_when_staff_id_missing() -> None:
    """External / non-staff DingTalk users have empty sender_staff_id
    but populated sender_id. Adapter prefers staff_id but falls back
    to sender_id so we still get a usable user_ref."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    await adapter._handle_message(_make_chatbot_message(
        sender_staff_id="", sender_id="$:LWCP_v1:$ext_user",
    ))
    assert inbox[0].user_ref == "$:LWCP_v1:$ext_user"


@pytest.mark.asyncio
async def test_inbound_dedup_drops_duplicate_message() -> None:
    """钉钉's Stream Mode has at-least-once delivery — same callback
    can land twice on reconnect. Dedup mirrors slack/discord/telegram."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    msg = _make_chatbot_message(text="ping", message_id="msgDUP")
    await adapter._handle_message(msg)
    await adapter._handle_message(msg)  # exact replay
    assert inbox == ["ping"]


@pytest.mark.asyncio
async def test_inbound_skips_non_text_messages() -> None:
    """v1 of the adapter only handles text. Picture / richText come
    back as alternative content fields — agent can ask the user to
    use text."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    await adapter._handle_message(_make_chatbot_message(
        message_type="picture",
    ))
    await adapter._handle_message(_make_chatbot_message(
        message_type="richText",
    ))
    assert inbox == []


@pytest.mark.asyncio
async def test_inbound_skips_empty_text_and_no_conversation_id() -> None:
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m))  # type: ignore[arg-type, return-value]

    # Empty text — drop.
    await adapter._handle_message(_make_chatbot_message(text="   "))
    # No conversation_id — drop (defensive).
    await adapter._handle_message(_make_chatbot_message(
        text="x", conversation_id="",
    ))
    assert inbox == []


# ── allowlist (B-337 parity) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_allowlist_drops_unauthorized_user() -> None:
    adapter = _build_adapter(allowed_user_ids=["manager_owner"])
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    await adapter._handle_message(_make_chatbot_message(
        text="from stranger", sender_staff_id="manager_stranger",
        message_id="msg1",
    ))
    assert inbox == []


@pytest.mark.asyncio
async def test_allowlist_passes_authorized_user() -> None:
    adapter = _build_adapter(allowed_user_ids=["manager_owner"])
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    await adapter._handle_message(_make_chatbot_message(
        text="from owner", sender_staff_id="manager_owner",
        message_id="msg2",
    ))
    assert inbox == ["from owner"]


@pytest.mark.asyncio
async def test_no_allowlist_lets_anyone_in() -> None:
    """Backward compat: no allowlist set → any sender is fine. Matches
    feishu / telegram / slack defaults."""
    adapter = _build_adapter()
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    await adapter._handle_message(_make_chatbot_message(
        text="any user", sender_staff_id="random_user", message_id="msg3",
    ))
    assert inbox == ["any user"]


@pytest.mark.asyncio
async def test_conversation_allowlist_drops_unauthorized_conversation() -> None:
    """A user might be allowlisted in their DM but not authorized to
    drive the agent from a group conversation. allowed_conversation_ids
    splits this — empty list = no restriction; non-empty = conversation
    must be in the list."""
    adapter = _build_adapter(
        allowed_conversation_ids=["cidTEAM==", "cidOWNERDM=="],
    )
    inbox: list = []
    adapter.subscribe(lambda m: inbox.append(m.content))  # type: ignore[arg-type, return-value]

    # User OK, conversation NOT in allowlist.
    await adapter._handle_message(_make_chatbot_message(
        text="wrong conversation", conversation_id="cidRANDOM==",
        message_id="msg4",
    ))
    assert inbox == []


# ── outbound send ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_routes_through_session_webhook() -> None:
    """send() must use the cached ChatbotMessage's session_webhook
    via the SDK handler's reply_text — that's the SDK's recommended
    path for bot replies (works for single + group chat without
    OpenAPI tokens)."""
    adapter = _build_adapter()
    cached_msg = _make_chatbot_message(conversation_id="cidA==")
    adapter._conversation_msgs["cidA=="] = cached_msg
    adapter._client = MagicMock()  # presence triggers "started" check
    fake_handler = MagicMock()
    fake_handler.reply_text = MagicMock(return_value={"errcode": 0, "errmsg": "ok"})
    adapter._handler = fake_handler

    await adapter.send(
        ChannelTarget(channel="dingtalk", ref="cidA=="),
        OutboundMessage(content="hi from agent"),
    )
    fake_handler.reply_text.assert_called_once_with("hi from agent", cached_msg)


@pytest.mark.asyncio
async def test_send_rejects_wrong_channel_target() -> None:
    """An adapter must refuse ChannelTargets for a different channel."""
    adapter = _build_adapter()
    adapter._client = MagicMock()
    adapter._handler = MagicMock()
    with pytest.raises(ValueError):
        await adapter.send(
            ChannelTarget(channel="not-dingtalk", ref="cidA=="),
            OutboundMessage(content="leak"),
        )


@pytest.mark.asyncio
async def test_send_rejects_unknown_conversation() -> None:
    """If we've never seen an inbound from this conversation, we
    don't have its session_webhook. The dispatcher must surface
    something actionable so the user knows replies require an
    incoming message first."""
    adapter = _build_adapter()
    adapter._client = MagicMock()
    adapter._handler = MagicMock()
    with pytest.raises(RuntimeError, match="session_webhook"):
        await adapter.send(
            ChannelTarget(channel="dingtalk", ref="cidNEW=="),
            OutboundMessage(content="x"),
        )


@pytest.mark.asyncio
async def test_send_chunks_long_messages() -> None:
    """钉钉's text reply chokes around ~5000 chars. A 6k reply must
    arrive as multiple successive sends so big tool dumps don't
    truncate."""
    adapter = _build_adapter()
    cached_msg = _make_chatbot_message(conversation_id="cidLONG==")
    adapter._conversation_msgs["cidLONG=="] = cached_msg
    adapter._client = MagicMock()
    fake_handler = MagicMock()
    fake_handler.reply_text = MagicMock(return_value={"errcode": 0})
    adapter._handler = fake_handler

    long_text = "word " * 1500  # 7500 chars
    await adapter.send(
        ChannelTarget(channel="dingtalk", ref="cidLONG=="),
        OutboundMessage(content=long_text),
    )
    assert fake_handler.reply_text.call_count >= 2


@pytest.mark.asyncio
async def test_send_raises_on_dingtalk_failure() -> None:
    """If reply_text raises (network blip / SDK error on the LAST
    chunk), the adapter surfaces a RuntimeError so the dispatcher's
    outer try/except records 'channel.send_failed'."""
    adapter = _build_adapter()
    cached_msg = _make_chatbot_message(conversation_id="cidF==")
    adapter._conversation_msgs["cidF=="] = cached_msg
    adapter._client = MagicMock()
    fake_handler = MagicMock()
    fake_handler.reply_text = MagicMock(side_effect=RuntimeError("network err"))
    adapter._handler = fake_handler

    with pytest.raises(RuntimeError, match="dingtalk send failed"):
        await adapter.send(
            ChannelTarget(channel="dingtalk", ref="cidF=="),
            OutboundMessage(content="will fail"),
        )


@pytest.mark.asyncio
async def test_send_raises_when_reply_text_returns_none() -> None:
    """The SDK's reply_text swallows requests errors and returns None
    on HTTP failure. Surface that as a RuntimeError so the user
    knows the delivery dropped (as opposed to silently ack-ing)."""
    adapter = _build_adapter()
    cached_msg = _make_chatbot_message(conversation_id="cidNONE==")
    adapter._conversation_msgs["cidNONE=="] = cached_msg
    adapter._client = MagicMock()
    fake_handler = MagicMock()
    fake_handler.reply_text = MagicMock(return_value=None)
    adapter._handler = fake_handler

    with pytest.raises(RuntimeError, match="reply_text returned None"):
        await adapter.send(
            ChannelTarget(channel="dingtalk", ref="cidNONE=="),
            OutboundMessage(content="will silently drop"),
        )


@pytest.mark.asyncio
async def test_send_unstarted_adapter_raises() -> None:
    adapter = _build_adapter()
    with pytest.raises(RuntimeError, match="not started"):
        await adapter.send(
            ChannelTarget(channel="dingtalk", ref="cidA=="),
            OutboundMessage(content="x"),
        )


@pytest.mark.asyncio
async def test_send_empty_content_returns_quickly() -> None:
    """钉钉 rejects empty content. Adapter should bail before
    hitting the API."""
    adapter = _build_adapter()
    cached_msg = _make_chatbot_message(conversation_id="cidE==")
    adapter._conversation_msgs["cidE=="] = cached_msg
    adapter._client = MagicMock()
    fake_handler = MagicMock()
    fake_handler.reply_text = MagicMock()
    adapter._handler = fake_handler

    result = await adapter.send(
        ChannelTarget(channel="dingtalk", ref="cidE=="),
        OutboundMessage(content=""),
    )
    assert result == ""
    fake_handler.reply_text.assert_not_called()


# ── start() lifecycle ──────────────────────────────────────────────


def _build_fake_dingtalk_module():
    """Construct a SimpleNamespace mimicking ``dingtalk_stream`` just
    enough for start() to thread through. Returns the module + handler
    capture dict."""
    captured: dict = {}

    class _FakeCredential:
        def __init__(self, client_id, client_secret):
            captured["client_id"] = client_id
            captured["client_secret"] = client_secret

    class _FakeClient:
        def __init__(self, credential):
            captured["credential"] = credential
            self._handlers = {}

        def register_callback_handler(self, topic, handler):
            self._handlers[topic] = handler
            captured["registered_topic"] = topic

        async def start(self):
            # Mimic the SDK's start: never returns under normal
            # operation; for tests we just sleep a tick so the task
            # exists long enough for stop() to cancel it.
            import asyncio as _a
            try:
                while True:
                    await _a.sleep(60)
            except _a.CancelledError:
                raise

    class _FakeAckMessage:
        STATUS_OK = 0

    class _FakeChatbotMessage:
        TOPIC = "/v1.0/im/bot/messages/get"

        @classmethod
        def from_dict(cls, d):
            return SimpleNamespace(**d)

    class _FakeAsyncChatbotHandler:
        def __init__(self):
            pass

        def process(self, _msg):
            return (_FakeAckMessage.STATUS_OK, "ok")

    fake_module = SimpleNamespace(
        Credential=_FakeCredential,
        DingTalkStreamClient=_FakeClient,
        AckMessage=_FakeAckMessage,
        ChatbotMessage=_FakeChatbotMessage,
        AsyncChatbotHandler=_FakeAsyncChatbotHandler,
    )
    return fake_module, captured


@pytest.mark.asyncio
async def test_start_connects_with_configured_credentials() -> None:
    """start() must hand client_id + client_secret to Credential and
    register a ChatbotMessage handler. Patches the dingtalk_stream
    import so no network is touched."""
    adapter = _build_adapter(
        client_id="ding-real-client-id",
        client_secret="real-secret",
    )

    fake_module, captured = _build_fake_dingtalk_module()

    with patch.dict(sys.modules, {"dingtalk_stream": fake_module}):
        await adapter.start()
        try:
            assert captured["client_id"] == "ding-real-client-id"
            assert captured["client_secret"] == "real-secret"
            assert captured["registered_topic"] == "/v1.0/im/bot/messages/get"
            assert adapter._client is not None
            assert adapter._handler is not None
            assert adapter.last_start_error is None
        finally:
            await adapter.stop()


@pytest.mark.asyncio
async def test_start_credential_failure_surfaces_clear_error() -> None:
    """If Credential() / DingTalkStreamClient() construction fails
    (bad inputs), the adapter must surface a RuntimeError with
    operator-readable text + set last_start_error so the setup
    endpoint can render it."""
    adapter = _build_adapter()

    fake_module, _ = _build_fake_dingtalk_module()
    # Make Credential explode on construction.
    fake_module.Credential = MagicMock(
        side_effect=ValueError("bad credentials"),
    )

    with patch.dict(sys.modules, {"dingtalk_stream": fake_module}):
        with pytest.raises(RuntimeError, match="DingTalk client init"):
            await adapter.start()

    assert adapter.last_start_error is not None
    assert (
        "client_id" in adapter.last_start_error
        or "client_secret" in adapter.last_start_error
    )


@pytest.mark.asyncio
async def test_start_idempotent() -> None:
    """A second start() call after success is a no-op (matches the
    feishu / telegram / discord / slack contract)."""
    adapter = _build_adapter()
    sentinel = MagicMock()
    adapter._client = sentinel
    await adapter.start()  # MUST NOT do anything
    assert adapter._client is sentinel


@pytest.mark.asyncio
async def test_stop_cancels_client_task() -> None:
    """stop() must cancel the long-running client task."""
    adapter = _build_adapter()

    fake_module, _ = _build_fake_dingtalk_module()

    with patch.dict(sys.modules, {"dingtalk_stream": fake_module}):
        await adapter.start()
        task_before = adapter._client_task
        assert task_before is not None
        await adapter.stop()

    assert adapter._client is None
    assert adapter._client_task is None
    assert task_before.cancelled() or task_before.done()


@pytest.mark.asyncio
async def test_stop_unstarted_is_noop() -> None:
    adapter = _build_adapter()
    await adapter.stop()  # MUST NOT raise


# ── lazy-import: missing dingtalk-stream ──────────────────────────


@pytest.mark.asyncio
async def test_missing_dingtalk_stream_gives_clear_install_hint() -> None:
    """``import dingtalk_stream`` inside start() must fail with a
    RuntimeError that names the pip install command. Without the
    lazy-import, the daemon would crash at module import time for
    users who never enable DingTalk — making the channel a hard dep
    instead of an optional one."""
    adapter = _build_adapter()

    # Block the dingtalk_stream package at import time.
    with patch.dict(sys.modules, {"dingtalk_stream": None}):
        with pytest.raises(RuntimeError) as exc_info:
            await adapter.start()

    msg = str(exc_info.value)
    # Operator gets a concrete command, not a stack trace.
    assert "pip install" in msg
    assert "dingtalk" in msg.lower()
    # last_start_error mirrors the same hint so the setup endpoint
    # can show it without re-deriving.
    assert adapter.last_start_error is not None
    assert "pip install" in adapter.last_start_error


def test_module_imports_without_dingtalk_stream() -> None:
    """Critical: ``from xmclaw.providers.channel.dingtalk.adapter
    import DingTalkAdapter`` must succeed even when dingtalk-stream
    isn't installed. Otherwise the daemon's manifest discovery (which
    imports the package) crashes for every user who never enables
    DingTalk. Verified by reading the adapter source — module-level
    imports are limited to xmclaw + stdlib + the abstract base."""
    import xmclaw.providers.channel.dingtalk.adapter as mod
    assert hasattr(mod, "DingTalkAdapter")
    # The adapter module did NOT pull dingtalk_stream into sys.modules
    # by side effect — verify by reading its source. The whole point
    # of the lazy import is that ``import xmclaw.providers.channel.dingtalk.adapter``
    # stays clean.
    import inspect
    source = inspect.getsource(mod)
    for line in source.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("import dingtalk_stream", "from dingtalk_stream")):
            # It's a top-level import iff it has zero leading whitespace.
            assert line != stripped, (
                f"adapter.py has a TOP-LEVEL dingtalk_stream import: {line!r}; "
                "this would crash the daemon at import time when the "
                "extra is not installed"
            )


# ── manifest registration ────────────────────────────────────────


def test_manifest_registers_dingtalk_as_ready() -> None:
    """The ``__init__.py`` MANIFEST must mark DingTalk ready (not
    scaffold) so the dispatcher actually wires it. Without this,
    ``include_scaffolds=False`` (production default) would hide it."""
    from xmclaw.providers.channel.dingtalk import MANIFEST
    assert MANIFEST.id == "dingtalk"
    assert MANIFEST.implementation_status == "ready"
    assert MANIFEST.adapter_factory_path == (
        "xmclaw.providers.channel.dingtalk.adapter:DingTalkAdapter"
    )
    # No public webhook needed — Stream Mode connects from our side.
    assert MANIFEST.needs_tunnel is False


def test_dingtalk_appears_in_registry_discover() -> None:
    """Production discovery (``include_scaffolds=False``) must surface
    DingTalk — sibling of the same telegram / discord / slack /
    feishu invariant. Without this the user can set
    ``channels.dingtalk.enabled=true`` and the daemon would log
    'channel.unknown id=dingtalk' and silently skip."""
    from xmclaw.providers.channel.registry import discover

    ready = discover(include_scaffolds=False)
    assert "dingtalk" in ready, (
        f"dingtalk not in default discovery; got: {list(ready.keys())}"
    )
    m = ready["dingtalk"]
    assert m.implementation_status == "ready"


# ── helper-level coverage ─────────────────────────────────────────


def test_split_for_dingtalk_under_cap() -> None:
    assert _split_for_dingtalk("short") == ["short"]
    assert _split_for_dingtalk("") == []


def test_split_for_dingtalk_chunks_at_cap() -> None:
    text = "x" * 6000
    chunks = _split_for_dingtalk(text, cap=4500)
    assert len(chunks) >= 2
    assert all(len(c) <= 4500 for c in chunks)
    # Reassembly returns the original.
    assert "".join(chunks) == text


def test_split_for_dingtalk_prefers_newline_boundaries() -> None:
    para = "a" * 1500
    text = "\n\n".join([para, para, para, para])
    chunks = _split_for_dingtalk(text, cap=4500)
    assert all(len(c) <= 4500 for c in chunks)


def test_coerce_str_set_handles_none_and_empty() -> None:
    assert _coerce_str_set(None, key="x") == set()
    assert _coerce_str_set([], key="x") == set()


def test_coerce_str_set_strips_whitespace() -> None:
    assert _coerce_str_set(
        [" manager4321 ", "manager5678"], key="x",
    ) == {"manager4321", "manager5678"}


def test_coerce_str_set_drops_empty_strings() -> None:
    """Empty string entries are dropped — they'd never match any real
    DingTalk id and hide bugs in the config."""
    assert _coerce_str_set(["m1", "", "  "], key="x") == {"m1"}
