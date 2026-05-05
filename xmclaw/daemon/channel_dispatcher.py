"""ChannelDispatcher — bridge inbound channel messages to AgentLoop.

B-145. The piece that turns a channel adapter (飞书 / 钉钉 / etc.)
from "logs that a message came in" into "agent answers the user in
the same chat".

Flow per inbound:

  channel_adapter.subscribe(handler) → handler(InboundMessage)
    → ChannelDispatcher._on_inbound:
        1. session_id = "<channel>:<chat_ref>"  (stable per chat)
        2. publish a USER_MESSAGE-equivalent into the bus so the
           Trace / Insights pages see it
        3. await agent.run_turn(session_id, content)
        4. capture the assistant's final text from the turn
        5. adapter.send(target, OutboundMessage(text, reply_to=msg_id))

Per-channel session id keeps history isolated: a 飞书 group chat and
a daemon REPL are different sessions. Same chat across daemon
restarts gets the same session_id, so the agent has continuity.

Why session-store + agent.run_turn rather than a fresh AgentLoop per
message: turn history compaction, persona file injection, all the
existing memory machinery come along for free. This is the same
agent the user talks to via the web UI.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from xmclaw.providers.channel.base import (
    ChannelAdapter,
    InboundMessage,
    OutboundMessage,
)


_log = logging.getLogger(__name__)


class ChannelDispatcher:
    """Owns N channel adapters, routes their inbound to one agent.

    Args:
        agent: an :class:`AgentLoop`-like object with ``run_turn`` and
               an ``_histories`` dict (used to fish out the final
               assistant text after the turn). Duck-typed via Protocol
               so this module doesn't import xmclaw.daemon.agent_loop.
    """

    def __init__(self, agent: Any, *, ack_delay_s: float = 0.0) -> None:
        self._agent = agent
        self._adapters: list[ChannelAdapter] = []
        # In-flight per-(channel, chat) lock so two messages in the
        # same chat don't trample each other's turns.
        self._chat_locks: dict[str, asyncio.Lock] = {}
        # B-195: how long to wait before sending the "thinking..."
        # placeholder. Default 0 — user wants immediate ack on every
        # message ("不要两秒，要立刻"). Set >0 to suppress ack on
        # fast turns, or for tests that don't want the placeholder.
        self._ack_delay_s = ack_delay_s

    def add(self, adapter: ChannelAdapter) -> None:
        """Register an adapter + subscribe to its inbound stream."""
        adapter.subscribe(self._on_inbound)
        self._adapters.append(adapter)

    async def start_all(self) -> None:
        """Start every registered adapter. Adapter failures log + skip
        — one bad credential shouldn't break the others."""
        for a in self._adapters:
            try:
                await a.start()
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "channel.start_failed adapter=%s err=%s",
                    a.name, exc,
                )

    async def stop_all(self) -> None:
        for a in self._adapters:
            try:
                await a.stop()
            except Exception:  # noqa: BLE001
                pass

    # ── inbound routing ────────────────────────────────────────

    async def _on_inbound(self, msg: InboundMessage) -> None:
        """Called by every adapter when a user message arrives."""
        session_id = self._session_id_for(msg)
        lock = self._chat_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            try:
                await self._handle_one(msg, session_id)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "channel.dispatch_failed channel=%s err=%s",
                    msg.target.channel, exc,
                )

    async def _handle_one(self, msg: InboundMessage, session_id: str) -> None:
        agent = self._agent
        if agent is None or not hasattr(agent, "run_turn"):
            _log.warning("channel.no_agent_wired channel=%s", msg.target.channel)
            return

        adapter = next(
            (a for a in self._adapters if a.name == msg.target.channel),
            None,
        )
        reply_to = (msg.raw or {}).get("message_id") if msg.raw else None

        # B-195: delayed acknowledgement. Long-running turns
        # (web search + multi-hop LLM) can take 30s-2min in IM
        # channels — without feedback the user retries, then we get
        # duplicate-reply confusion. If the turn finishes in <2s,
        # we cancel the ack and just send the final answer; only
        # genuinely slow turns trigger the placeholder.
        async def _delayed_ack() -> None:
            await asyncio.sleep(self._ack_delay_s)
            if adapter is None:
                return
            try:
                await adapter.send(
                    msg.target,
                    OutboundMessage(
                        content="🌸 收到啦，正在思考中...",
                        reply_to=reply_to,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — best-effort
                _log.warning(
                    "channel.ack_failed channel=%s err=%s",
                    msg.target.channel, exc,
                )

        ack_task = asyncio.create_task(
            _delayed_ack(), name=f"channel-ack-{session_id[:32]}",
        )

        # Run a turn. agent.run_turn streams events to the bus AND
        # records the assistant's final text in agent._histories[sid].
        try:
            await agent.run_turn(session_id, msg.content)
        except Exception as exc:  # noqa: BLE001
            _log.warning("channel.run_turn_failed err=%s", exc)
            ack_task.cancel()
            return
        finally:
            ack_task.cancel()
            # Swallow the cancelled task cleanly so it doesn't surface
            # as an unhandled exception warning.
            try:
                await ack_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        # Pull the most recent assistant text from history.
        reply_text = self._extract_last_assistant(agent, session_id)
        if not reply_text:
            return

        if adapter is None:
            return

        # B-199: scan the assistant's reply for local image paths and
        # auto-attach them. Triggered the chat-2026-05-03 17:51 issue
        # where the agent saved a screenshot, told the user the path,
        # but didn't actually send the image — the channel had no way
        # to "send a file". OutboundMessage.attachments has been there
        # all along; this just extracts paths the agent already named.
        attachments = _extract_local_image_paths(reply_text)
        try:
            await adapter.send(
                msg.target,
                OutboundMessage(
                    content=reply_text,
                    reply_to=reply_to,
                    attachments=tuple(attachments),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "channel.send_failed channel=%s err=%s",
                msg.target.channel, exc,
            )

    # ── helpers ────────────────────────────────────────────────

    def _session_id_for(self, msg: InboundMessage) -> str:
        """Stable session id per (channel, chat). Same chat across
        daemon restarts → same id → conversation history continuity."""
        return f"{msg.target.channel}:{msg.target.ref}"

    def _extract_last_assistant(self, agent: Any, session_id: str) -> str:
        """Reach into agent._histories to pull the final assistant
        message content. Same trick agent_inter.py uses.

        Tolerates both Message dataclass shape and plain dict shape so
        a future history refactor doesn't silently break channel reply.
        """
        histories = getattr(agent, "_histories", None) or {}
        history = histories.get(session_id) or []
        for entry in reversed(history):
            role = (
                getattr(entry, "role", None)
                or (entry.get("role") if isinstance(entry, dict) else None)
            )
            if role != "assistant":
                continue
            content = (
                getattr(entry, "content", None)
                or (entry.get("content") if isinstance(entry, dict) else None)
            )
            if isinstance(content, str) and content.strip():
                return content
        return ""


# B-199: scan reply text for local image paths and surface them as
# OutboundMessage.attachments. Triggered by the chat-2026-05-03 17:51
# bug — agent took a screenshot, named the path in its reply, then
# refused to "send" because the channel adapter only knew how to
# emit text. The fix is two-sided: adapter learns to send images
# (FeishuAdapter.send accepts attachments), dispatcher learns to
# detect implicit attachments in reply text.

_IMAGE_PATH_RE = re.compile(
    r"""
    (?P<path>
        (?:[A-Za-z]:)?              # optional Windows drive
        [\\/][\w\-./\\: ]+           # at least one separator + path body
        \.(?:png|jpg|jpeg|gif|webp|bmp)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _extract_local_image_paths(text: str, *, max_paths: int = 4) -> list[str]:
    """Pull existing local image file paths out of ``text``.

    Returns up to ``max_paths`` deduplicated paths that exist on
    disk and are images by extension. Hardens against:
    - paths inside backticks / quotes (the regex matches the path
      content, callers already saw the markup)
    - duplicated mentions (dedup preserves first-seen order)
    - non-existent paths (skipped — agent might be hallucinating)

    Defensive size cap so a malicious reply can't try to attach
    100 files.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _IMAGE_PATH_RE.finditer(text):
        raw = (m.group("path") or "").strip().strip("`'\" ")
        if not raw or raw in seen:
            continue
        if not os.path.isfile(raw):
            continue
        seen.add(raw)
        out.append(raw)
        if len(out) >= max_paths:
            break
    return out
