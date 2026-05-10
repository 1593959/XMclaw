"""B-384 (Sprint 2): WeComAdapter unit tests.

Pre-B-384 the wecom package was a 19-line scaffold raising
NotImplementedError on construction. The real adapter is **outbound-
only** — WeCom internal-bot webhooks are one-way (we POST notifications
to the group; there is no inbound delivery for this surface). Inbound
requires the self-built-app callback, which needs a public URL and is
out-of-scope until the cloudflared bootstrap lands generically.

Test posture mirrors :file:`test_v2_channels_telegram.py` /
:file:`test_v2_channels_slack.py`: instantiate with fake config + drive
``send`` through a mocked ``httpx.AsyncClient``. We do NOT touch the
network; ``post`` is mocked at the client instance level.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from xmclaw.providers.channel.base import (
    ChannelTarget,
    InboundMessage,
    OutboundMessage,
)
from xmclaw.providers.channel._shared import split_text
from xmclaw.providers.channel.wecom.adapter import (
    WeComAdapter,
    _coerce_str_list,
    _validate_webhook_url,
)


# ── helpers ────────────────────────────────────────────────────────


_FAKE_WEBHOOK = (
    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
    "?key=00000000-0000-0000-0000-000000000001"
)


def _build_adapter(**extra_cfg: Any) -> WeComAdapter:
    """Construct without start() — exercises only validation + helpers.
    ``webhook_url`` is required at __init__ so we always pass it."""
    cfg: dict[str, Any] = {"webhook_url": _FAKE_WEBHOOK}
    cfg.update(extra_cfg)
    return WeComAdapter(cfg)


def _make_response(
    *,
    status: int = 200,
    json_body: dict[str, Any] | None = None,
    text: str = "",
) -> MagicMock:
    """Build a fake httpx.Response stand-in. Only the attrs the adapter
    reads are populated."""
    resp = MagicMock()
    resp.status_code = status
    if json_body is not None:
        resp.json = MagicMock(return_value=json_body)
    else:
        resp.json = MagicMock(side_effect=ValueError("not json"))
    resp.text = text or (
        "" if json_body is None else "json-body-omitted-for-mock"
    )
    return resp


# ── construction + config validation ───────────────────────────────


def test_adapter_requires_webhook_url() -> None:
    """No webhook_url → can't post anywhere; surface the install hint
    rather than booting and dying on first send()."""
    with pytest.raises(ValueError, match="webhook_url"):
        WeComAdapter({})


def test_adapter_rejects_non_https_url() -> None:
    """Mistyped URL like 'qyapi.weixin.qq.com/...' (no scheme) — catch
    at boot rather than silently letting httpx try to POST a relative
    path."""
    with pytest.raises(ValueError, match="https://"):
        WeComAdapter({"webhook_url": "qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x"})


def test_adapter_rejects_wrong_host_url() -> None:
    """Pasting a Feishu webhook into the WeCom field is the most likely
    misconfig; catch the wrong host explicitly with a hint."""
    with pytest.raises(ValueError, match="qyapi.weixin.qq.com"):
        WeComAdapter({
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abcd",
        })


def test_adapter_rejects_url_missing_key() -> None:
    """The bot key is the only authentication WeCom webhooks have;
    a URL without 'key=' is meaningless."""
    with pytest.raises(ValueError, match="key="):
        WeComAdapter({
            "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
        })


def test_adapter_default_msgtype_is_markdown() -> None:
    """Markdown is the most useful default — the agent tends to emit
    fenced code blocks, headers, and lists that markdown renders well."""
    adapter = _build_adapter()
    assert adapter._msgtype == "markdown"


def test_adapter_accepts_text_msgtype() -> None:
    adapter = _build_adapter(msgtype="text")
    assert adapter._msgtype == "text"


def test_adapter_rejects_unknown_msgtype() -> None:
    """Typo'd msgtype like 'markdwon' — surface at boot."""
    with pytest.raises(ValueError, match="msgtype"):
        _build_adapter(msgtype="markdwon")


def test_adapter_rejects_non_string_msgtype() -> None:
    with pytest.raises(ValueError, match="msgtype"):
        _build_adapter(msgtype=42)


def test_adapter_accepts_mention_lists() -> None:
    """userid + mobile lists are honored at @mention construction time
    (text msgtype only)."""
    adapter = _build_adapter(
        msgtype="text",
        mentioned_list=["@all"],
        mentioned_mobile_list=["13800138000"],
    )
    assert adapter._mentioned_list == ["@all"]
    assert adapter._mentioned_mobile_list == ["13800138000"]


def test_adapter_rejects_string_mention_list() -> None:
    """A common typo: ``mentioned_list: "@all"`` (string, not list) —
    catch at __init__ rather than silently treating it as a 4-char list."""
    with pytest.raises(ValueError, match="mentioned_list"):
        _build_adapter(mentioned_list="@all")


def test_adapter_rejects_non_string_in_mention_list() -> None:
    """A ``[12345]`` typo would silently send int via JSON — WeCom's
    validator rejects this with 40058 'invalid mentioned_list'."""
    with pytest.raises(ValueError, match="must be str"):
        _build_adapter(mentioned_list=[12345])


# ── outbound send ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_posts_markdown_body_to_webhook() -> None:
    """send() must POST a markdown body shaped per WeCom's docs:
    {msgtype: 'markdown', markdown: {content: ...}}."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=_make_response(json_body={"errcode": 0, "errmsg": "ok"}),
    )
    adapter._client = fake_client

    msgid = await adapter.send(
        ChannelTarget(channel="wecom", ref="group1"),
        OutboundMessage(content="hello **world**"),
    )

    fake_client.post.assert_awaited_once()
    args, kwargs = fake_client.post.call_args
    # First positional arg is the URL.
    assert args[0] == _FAKE_WEBHOOK
    body = kwargs["json"]
    assert body == {
        "msgtype": "markdown",
        "markdown": {"content": "hello **world**"},
    }
    assert msgid.startswith("wecom:")


@pytest.mark.asyncio
async def test_send_text_msgtype_includes_mentions() -> None:
    """Text msgtype carries ``mentioned_list`` / ``mentioned_mobile_list``
    inside the inner ``text`` object — only on the first chunk so
    subsequent chunks don't re-tag everyone."""
    adapter = _build_adapter(
        msgtype="text",
        mentioned_list=["U_alice", "@all"],
        mentioned_mobile_list=["13800138000"],
    )
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=_make_response(json_body={"errcode": 0, "errmsg": "ok"}),
    )
    adapter._client = fake_client

    await adapter.send(
        ChannelTarget(channel="wecom", ref="group1"),
        OutboundMessage(content="@alice fix the build"),
    )

    body = fake_client.post.call_args.kwargs["json"]
    assert body["msgtype"] == "text"
    assert body["text"]["content"] == "@alice fix the build"
    assert body["text"]["mentioned_list"] == ["U_alice", "@all"]
    assert body["text"]["mentioned_mobile_list"] == ["13800138000"]


@pytest.mark.asyncio
async def test_send_chunks_long_message_at_4096() -> None:
    """WeCom caps each message at 4096 chars (errcode 93000 above that).
    Long content must be chunked into successive POSTs."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=_make_response(json_body={"errcode": 0, "errmsg": "ok"}),
    )
    adapter._client = fake_client

    long_text = "word " * 1500  # 7500 chars >> cap
    await adapter.send(
        ChannelTarget(channel="wecom", ref="group1"),
        OutboundMessage(content=long_text),
    )

    # At least 2 calls (>4096 chars total → ≥2 chunks).
    assert fake_client.post.await_count >= 2
    for call in fake_client.post.call_args_list:
        body = call.kwargs["json"]
        assert len(body["markdown"]["content"]) <= 4096


@pytest.mark.asyncio
async def test_send_rejects_wrong_channel_target() -> None:
    """An adapter must refuse ChannelTargets for a different channel —
    the dispatcher routes by name; a mismatched target is a wiring bug."""
    adapter = _build_adapter()
    adapter._client = MagicMock()
    with pytest.raises(ValueError):
        await adapter.send(
            ChannelTarget(channel="not-wecom", ref="x"),
            OutboundMessage(content="leak"),
        )


@pytest.mark.asyncio
async def test_send_unstarted_adapter_raises() -> None:
    adapter = _build_adapter()
    with pytest.raises(RuntimeError, match="not started"):
        await adapter.send(
            ChannelTarget(channel="wecom", ref="group1"),
            OutboundMessage(content="x"),
        )


@pytest.mark.asyncio
async def test_send_empty_content_returns_quietly() -> None:
    """WeCom's webhook returns errcode 44004 on empty body; bail before
    the API hit and mirror Slack's posture."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.post = AsyncMock()
    adapter._client = fake_client

    result = await adapter.send(
        ChannelTarget(channel="wecom", ref="group1"),
        OutboundMessage(content=""),
    )
    assert result == ""
    fake_client.post.assert_not_called()


@pytest.mark.asyncio
async def test_send_raises_on_wecom_errcode() -> None:
    """WeCom returns 200 OK with errcode != 0 for logical errors
    (rate limit, frequency cap, disabled bot). The adapter must surface
    these as RuntimeError so the dispatcher's outer try/except records
    channel.send_failed instead of silently dropping."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=_make_response(
            status=200,
            json_body={"errcode": 45009, "errmsg": "frequency limit exceeded"},
        ),
    )
    adapter._client = fake_client

    with pytest.raises(RuntimeError, match="errcode=45009"):
        await adapter.send(
            ChannelTarget(channel="wecom", ref="group1"),
            OutboundMessage(content="rate limited please"),
        )


@pytest.mark.asyncio
async def test_send_retries_once_on_5xx() -> None:
    """Transient 5xx (deploy windows, hiccups) deserve one retry. Two
    consecutive 5xx fails → RuntimeError."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    # First call: 503; second call: 200 success.
    fake_client.post = AsyncMock(
        side_effect=[
            _make_response(status=503),
            _make_response(json_body={"errcode": 0, "errmsg": "ok"}),
        ],
    )
    adapter._client = fake_client

    msgid = await adapter.send(
        ChannelTarget(channel="wecom", ref="group1"),
        OutboundMessage(content="ok now"),
    )

    assert fake_client.post.await_count == 2
    assert msgid.startswith("wecom:")


@pytest.mark.asyncio
async def test_send_persistent_5xx_raises() -> None:
    """Two consecutive 5xx fails → RuntimeError; we don't retry forever."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        side_effect=[
            _make_response(status=503),
            _make_response(status=503),
        ],
    )
    adapter._client = fake_client

    with pytest.raises(RuntimeError, match="HTTP 503"):
        await adapter.send(
            ChannelTarget(channel="wecom", ref="group1"),
            OutboundMessage(content="will fail"),
        )
    assert fake_client.post.await_count == 2


@pytest.mark.asyncio
async def test_send_does_not_retry_4xx() -> None:
    """4xx is a client-side mistake; retrying with the same body just
    burns the rate limit. Surface immediately."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=_make_response(status=400, text="bad json"),
    )
    adapter._client = fake_client

    with pytest.raises(RuntimeError, match="HTTP 400"):
        await adapter.send(
            ChannelTarget(channel="wecom", ref="group1"),
            OutboundMessage(content="bad payload"),
        )
    assert fake_client.post.await_count == 1


@pytest.mark.asyncio
async def test_send_retries_once_on_network_error() -> None:
    """Network blip (ConnectError, ReadTimeout) → retry once. Second
    success → return cleanly."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        side_effect=[
            httpx.ConnectError("dns refused"),
            _make_response(json_body={"errcode": 0, "errmsg": "ok"}),
        ],
    )
    adapter._client = fake_client

    await adapter.send(
        ChannelTarget(channel="wecom", ref="group1"),
        OutboundMessage(content="recovered"),
    )
    assert fake_client.post.await_count == 2


@pytest.mark.asyncio
async def test_send_persistent_network_error_raises() -> None:
    """Two consecutive network errors → operator-readable RuntimeError."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        side_effect=[
            httpx.ConnectError("blip 1"),
            httpx.ConnectError("blip 2"),
        ],
    )
    adapter._client = fake_client

    with pytest.raises(RuntimeError, match="network error"):
        await adapter.send(
            ChannelTarget(channel="wecom", ref="group1"),
            OutboundMessage(content="will fail"),
        )


@pytest.mark.asyncio
async def test_send_chunked_text_only_first_chunk_carries_mentions() -> None:
    """Multi-chunk text sends must NOT re-tag @all on every chunk —
    that would spam notifications. Only chunk 0 carries the mention
    fields."""
    adapter = _build_adapter(
        msgtype="text",
        mentioned_list=["@all"],
    )
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=_make_response(json_body={"errcode": 0, "errmsg": "ok"}),
    )
    adapter._client = fake_client

    long_text = "x " * 3000  # 6000 chars → 2 chunks
    await adapter.send(
        ChannelTarget(channel="wecom", ref="group1"),
        OutboundMessage(content=long_text),
    )

    calls = fake_client.post.call_args_list
    assert len(calls) >= 2
    first_body = calls[0].kwargs["json"]
    assert first_body["text"].get("mentioned_list") == ["@all"]
    # Subsequent chunks must NOT carry mentions.
    for call in calls[1:]:
        assert "mentioned_list" not in call.kwargs["json"]["text"]


# ── lifecycle (start / stop) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_start_creates_async_client() -> None:
    """After start(), self._client is a real httpx.AsyncClient ready
    for posts. last_start_error is None on success."""
    adapter = _build_adapter()
    assert adapter._client is None

    await adapter.start()
    try:
        assert adapter._client is not None
        assert isinstance(adapter._client, httpx.AsyncClient)
        assert adapter.last_start_error is None
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_start_idempotent() -> None:
    """A second start() call after success is a no-op."""
    adapter = _build_adapter()
    sentinel = MagicMock()
    adapter._client = sentinel
    await adapter.start()  # MUST NOT do anything
    assert adapter._client is sentinel


@pytest.mark.asyncio
async def test_stop_closes_client_cleanly() -> None:
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.aclose = AsyncMock()
    adapter._client = fake_client

    await adapter.stop()
    fake_client.aclose.assert_awaited_once()
    assert adapter._client is None


@pytest.mark.asyncio
async def test_stop_unstarted_is_noop() -> None:
    adapter = _build_adapter()
    await adapter.stop()  # MUST NOT raise


@pytest.mark.asyncio
async def test_stop_swallows_close_error() -> None:
    """A failing aclose shouldn't prevent the adapter from clearing its
    state — daemon shutdown path keeps moving."""
    adapter = _build_adapter()
    fake_client = MagicMock()
    fake_client.aclose = AsyncMock(side_effect=RuntimeError("net err"))
    adapter._client = fake_client

    await adapter.stop()  # MUST NOT raise
    assert adapter._client is None


# ── inbound is documented out-of-scope ─────────────────────────────


@pytest.mark.asyncio
async def test_subscribe_is_noop_for_outbound_only_channel() -> None:
    """WeCom internal-bot webhooks are one-way. ``subscribe`` accepts
    handler registration to keep the ABC contract uniform across
    channels, but no inbound message is ever fanned out."""
    adapter = _build_adapter()
    inbox: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        inbox.append(msg)

    adapter.subscribe(handler)
    # The handler list is populated for ABC compliance but no fan-out
    # path exists. The dispatcher's wiring code can register and not
    # special-case WeCom.
    assert handler in adapter._handlers
    # No inbound delivery surface — verify by checking the adapter
    # has no driving entry point that calls into _handlers.
    # (We rely on the docstring + this assertion as the contract.)
    assert inbox == []


# ── manifest registration ────────────────────────────────────────


def test_wecom_appears_in_registry_discover() -> None:
    """The wecom package must register a ready-status manifest so
    ``discover()`` (used by the daemon's lifespan loop to wire enabled
    channels) sees it. Without this the user can set
    ``channels.wecom.enabled=true`` and the daemon would log
    'channel.unknown id=wecom' and silently skip."""
    from xmclaw.providers.channel.registry import discover
    manifests = discover()  # default include_scaffolds=False
    assert "wecom" in manifests
    m = manifests["wecom"]
    assert m.implementation_status == "ready"
    assert m.adapter_factory_path == (
        "xmclaw.providers.channel.wecom.adapter:WeComAdapter"
    )
    # Outbound-only webhook — no public URL needed for the daemon.
    assert m.needs_tunnel is False


# ── helper-level coverage ─────────────────────────────────────────


def test_split_for_wecom_under_cap() -> None:
    assert split_text("short", 4096) == ["short"]
    assert split_text("", 4096) == []


def test_split_for_wecom_chunks_at_cap() -> None:
    text = "x" * 5000
    chunks = split_text(text, cap=4096)
    assert len(chunks) >= 2
    assert all(len(c) <= 4096 for c in chunks)
    # Reassembly returns the original.
    assert "".join(chunks) == text


def test_split_for_wecom_prefers_newline_boundaries() -> None:
    para = "a" * 2000
    text = "\n\n".join([para, para, para])
    chunks = split_text(text, cap=4096)
    assert all(len(c) <= 4096 for c in chunks)


def test_coerce_str_list_handles_none_and_empty() -> None:
    assert _coerce_str_list(None, key="x") == []
    assert _coerce_str_list([], key="x") == []


def test_coerce_str_list_strips_whitespace() -> None:
    assert _coerce_str_list([" U123 ", "U456"], key="x") == ["U123", "U456"]


def test_coerce_str_list_drops_empty_strings() -> None:
    """Empty entries (`""`, `"   "`) are dropped — they'd never match
    a real WeCom userid and just hide bugs in the config."""
    assert _coerce_str_list(["U1", "", "  "], key="x") == ["U1"]


def test_validate_webhook_url_accepts_real_shape() -> None:
    """Sanity: the canonical webhook URL shape passes validation."""
    _validate_webhook_url(_FAKE_WEBHOOK)  # no raise


def test_validate_webhook_url_rejects_empty() -> None:
    with pytest.raises(ValueError, match="webhook_url"):
        _validate_webhook_url("")
