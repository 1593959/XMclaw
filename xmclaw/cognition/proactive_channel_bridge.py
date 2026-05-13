"""ProactiveChannelBridge — fan out proactive proposals to IM channels.

Sprint 2 Wave 9. ProactiveAgent publishes ``PROACTIVE_PROPOSAL`` events
on the bus when a trigger decides the agent should speak unprompted
(idle check-in, calendar reminder, stale project, etc.). The Web UI
already picks those up via its WS subscription, but a user who isn't
sitting at the screen will miss them.

This bridge subscribes to the same event and pushes the message into
the user's configured IM channels (飞书 / Telegram / Slack / …) so the
phone-side native push wakes them up.

Wiring
======

App lifespan constructs the bridge AFTER both ProactiveAgent and
ChannelDispatcher have started, passes it the event bus + a list of
``(adapter, target_ref)`` pairs read from
``config.channels.<id>.proactive_chat_id``. Each adapter that has a
chat id configured gets the proposal text. Adapters without a
configured target are skipped silently (you only want pushes in the
specific chat you opted into).

Config shape:

  channels:
    feishu:
      enabled: true
      app_id / app_secret: ...
      proactive_chat_id: "oc_xxxxxxxxxxxx"   ← target chat / DM ref

  Multiple channels can each opt in; the bridge fans out to all.

The bridge respects ``urgency``:
  * ``low``     — skipped (these don't need a phone push; they live
                  in the dashboard timeline)
  * ``normal``  — sent verbatim
  * ``high``    — prefixed with "🚨 " so it stands out in feishu's
                  notification preview

Filtering knobs (config.cognition.proactive.channel_push):
  * enabled: true|false                      (default true)
  * min_urgency: "low"|"normal"|"high"       (default "normal")
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from xmclaw.utils.log import get_logger

logger = get_logger(__name__)

_URGENCY_RANK = {"low": 0, "normal": 1, "high": 2}


@dataclass(slots=True)
class ChannelPushTarget:
    """One (adapter, chat-ref) tuple the bridge will push proposals to."""
    adapter: Any            # ChannelAdapter
    target_ref: str         # external chat/room id ("oc_xxxx" for feishu)


class ProactiveChannelBridge:
    """Bus subscriber that mirrors PROACTIVE_PROPOSAL events to IM channels.

    Idempotent across re-registration: each call to ``add_target``
    replaces an existing entry for the same (adapter.name, target_ref)
    pair.
    """

    def __init__(
        self,
        *,
        bus: Any,
        min_urgency: str = "normal",
        enabled: bool = True,
    ) -> None:
        self._bus = bus
        self._enabled = bool(enabled)
        self._min_urgency_rank = _URGENCY_RANK.get(min_urgency, 1)
        self._targets: list[ChannelPushTarget] = []
        self._subscription = None

    def add_target(self, adapter: Any, target_ref: str) -> None:
        if not target_ref or not isinstance(target_ref, str):
            return
        adapter_name = getattr(adapter, "name", "?")
        self._targets = [
            t for t in self._targets
            if not (
                getattr(t.adapter, "name", "?") == adapter_name
                and t.target_ref == target_ref
            )
        ]
        self._targets.append(ChannelPushTarget(
            adapter=adapter, target_ref=target_ref,
        ))

    def target_count(self) -> int:
        return len(self._targets)

    async def start(self) -> None:
        """Begin listening to the bus for PROACTIVE_PROPOSAL events."""
        if not self._enabled or not self._targets:
            logger.info(
                "proactive_channel_bridge.skipped enabled=%s targets=%d",
                self._enabled, len(self._targets),
            )
            return
        subscribe = getattr(self._bus, "subscribe", None)
        if not callable(subscribe):
            logger.warning(
                "proactive_channel_bridge.bus_has_no_subscribe",
            )
            return
        self._subscription = subscribe(
            self._predicate, self._on_event,
        )
        logger.info(
            "proactive_channel_bridge.started targets=%d",
            len(self._targets),
        )

    async def stop(self) -> None:
        sub = self._subscription
        if sub is None:
            return
        cancel = getattr(sub, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:  # noqa: BLE001
                pass
        self._subscription = None

    # ── internals ─────────────────────────────────────────────────

    @staticmethod
    def _predicate(event: Any) -> bool:
        # Match by string so we don't import EventType (keeps this
        # module free of core/bus dependency drift).
        t = getattr(event, "type", None)
        if hasattr(t, "value"):
            t = t.value
        return str(t) == "proactive_proposal"

    async def _on_event(self, event: Any) -> None:
        payload = getattr(event, "payload", None) or {}
        if not isinstance(payload, dict):
            return
        urgency = str(payload.get("urgency") or "normal").lower()
        if _URGENCY_RANK.get(urgency, 1) < self._min_urgency_rank:
            return
        message = str(payload.get("message") or "").strip()
        if not message:
            return
        prefix = "🚨 " if urgency == "high" else ""
        text = prefix + message
        # Fan out in parallel — one slow channel shouldn't hold up
        # others. Per-target failures log + skip.
        await asyncio.gather(
            *(self._send_one(t, text) for t in self._targets),
            return_exceptions=True,
        )

    async def _send_one(
        self, target: ChannelPushTarget, text: str,
    ) -> None:
        try:
            from xmclaw.providers.channel.base import (
                ChannelTarget,
                OutboundMessage,
            )
            ct = ChannelTarget(
                channel=getattr(target.adapter, "name", "?"),
                ref=target.target_ref,
            )
            om = OutboundMessage(content=text)
            await target.adapter.send(ct, om)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "proactive_channel_bridge.send_failed channel=%s err=%s",
                getattr(target.adapter, "name", "?"), exc,
            )


def build_bridge_from_config(
    *,
    bus: Any,
    channels_config: dict[str, Any] | None,
    proactive_push_config: dict[str, Any] | None,
    adapters: list[Any],
) -> ProactiveChannelBridge | None:
    """Convenience for lifespan: build + populate a bridge from the
    same config dicts already in scope.

    Returns ``None`` when there are no enabled channels with a
    ``proactive_chat_id`` — caller can skip starting the bridge.
    """
    pp_cfg = proactive_push_config or {}
    enabled = bool(pp_cfg.get("enabled", True))
    min_urgency = str(pp_cfg.get("min_urgency", "normal")).lower()
    bridge = ProactiveChannelBridge(
        bus=bus,
        min_urgency=min_urgency,
        enabled=enabled,
    )
    chcfg = channels_config or {}
    if not isinstance(chcfg, dict):
        return None
    for ch_id, ch_cfg in chcfg.items():
        if not isinstance(ch_cfg, dict) or not ch_cfg.get("enabled"):
            continue
        ref = ch_cfg.get("proactive_chat_id")
        if not isinstance(ref, str) or not ref.strip():
            continue
        adapter = next(
            (a for a in adapters if getattr(a, "name", None) == ch_id),
            None,
        )
        if adapter is None:
            continue
        bridge.add_target(adapter, ref.strip())
    if bridge.target_count() == 0:
        return None
    return bridge


__all__ = [
    "ProactiveChannelBridge",
    "ChannelPushTarget",
    "build_bridge_from_config",
]
