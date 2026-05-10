"""Shared utilities for LLM streaming implementations.

Extracted from anthropic.py / openai.py to eliminate duplication
in cancel-watchdog and stream-lifecycle patterns.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from xmclaw.utils.log import get_logger

log = get_logger(__name__)


async def _watch_cancel(
    cancel: asyncio.Event,
    close_stream: Callable[[], Awaitable[Any]],
) -> None:
    """B-225: close the stream the moment ``cancel`` fires.

    Without this, ``async for chunk in stream`` was suspended waiting
    for the server's next chunk and the in-loop ``cancel.is_set()``
    check never reached.
    """
    try:
        await cancel.wait()
        try:
            await close_stream()
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "%s failed during stream shutdown", type(exc).__name__,
                exc_info=True,
            )
    except asyncio.CancelledError:
        pass


def start_cancel_watchdog(
    cancel: asyncio.Event | None,
    close_stream: Callable[[], Awaitable[Any]],
) -> asyncio.Task[None] | None:
    """Start a background task that closes *stream* when *cancel* fires.

    Returns the task (or ``None`` when *cancel* is ``None``) so the
    caller can cancel + await it after the consume loop exits.
    """
    if cancel is None:
        return None
    return asyncio.create_task(_watch_cancel(cancel, close_stream))


async def stop_cancel_watchdog(
    task: asyncio.Task[None] | None,
) -> None:
    """Cancel and drain the watchdog task started by
    :func:`start_cancel_watchdog`.
    """
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
