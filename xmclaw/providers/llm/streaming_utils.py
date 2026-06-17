"""Shared utilities for LLM streaming implementations.

Extracted from anthropic.py / openai.py to eliminate duplication
in cancel-watchdog and stream-lifecycle patterns.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from xmclaw.utils.log import get_logger

log = get_logger(__name__)


_THINK_BLOCK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def split_inline_think(text: str) -> tuple[str, str]:
    """Separate inline ``<think>…</think>`` reasoning from visible content.

    2026-06-16: some models emit reasoning INLINE in the content as
    ``<think>…</think>`` rather than in a separate reasoning field. This is
    true for both OpenAI-compat models (DeepSeek-R1, …) AND Kimi k2.6 over
    its Anthropic-compat ``/coding`` endpoint — so both providers must strip
    it. Unstripped, the tag leaks into the chat bubble (user saw
    ``…创建看板页面。</think>现在我了解了…``). Returns ``(visible, thinking)``.
    Handles three shapes: balanced blocks, a leaked CLOSING tag with no
    opener (everything before the last ``</think>`` is reasoning), and a
    stray opener.
    """
    if "<think>" not in text and "</think>" not in text:
        return text, ""
    thinks = _THINK_BLOCK_RE.findall(text)  # inner reasoning of balanced blocks
    clean = _THINK_BLOCK_RE.sub("", text)
    if "</think>" in clean:  # leaked close (no opener): split at the last one
        idx = clean.rfind("</think>")
        thinks.append(clean[:idx])
        clean = clean[idx + len("</think>"):]
    clean = clean.replace("<think>", "").replace("</think>", "")
    extracted = "\n".join(t.strip() for t in thinks if t and t.strip())
    return clean.strip(), extracted


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _longest_partial_tag_suffix(buf: str) -> int:
    """Length of the longest suffix of ``buf`` that is a prefix of either
    think tag — i.e. a tag possibly split across the chunk boundary, which
    must be held back rather than forwarded as visible text."""
    best = 0
    for tag in (_THINK_OPEN, _THINK_CLOSE):
        # try the longest prefix of `tag` first
        for n in range(min(len(tag) - 1, len(buf)), 0, -1):
            if buf.endswith(tag[:n]):
                best = max(best, n)
                break
    return best


class InlineThinkStreamFilter:
    """Stateful splitter that strips inline ``<think>…</think>`` from a
    *streaming* text channel, chunk by chunk, so the reasoning never reaches
    the visible bubble live (the final-content re-strip alone is defeated by
    the webui reducer's "keep the longer of streamed/final" heuristic).

    ``feed(chunk)`` returns ``(visible, thinking)`` deltas to forward now.
    A short tail is buffered so a tag split across chunks (``"<thi"`` |
    ``"nk>"``) is still recognised. ``flush()`` drains any held tail at end
    of stream.
    """

    def __init__(self, hold_leading_reasoning: bool = False) -> None:
        self._in_think = False
        self._buf = ""
        # 2026-06-17: some endpoints (Kimi K2.6 /coding) emit reasoning at the
        # START of the text stream with NO opening <think>, then a lone
        # </think> before the answer. Token-by-token, the reasoning streams
        # out as visible before the close arrives — so dropping the literal
        # tag alone still leaks the reasoning sentence into the bubble. When
        # this flag is set we hold ALL leading text silently until the first
        # tag resolves it: a </think> → it was reasoning (→ thinking channel);
        # a <think> → the held text was real visible content; stream end with
        # neither → it was a plain answer (flush as visible). Once resolved,
        # normal streaming resumes. Gated to known leakers so real Claude
        # (which uses thinking_delta, never inline tags) keeps live streaming.
        self._hold_leading = hold_leading_reasoning
        self._lead_resolved = not hold_leading_reasoning

    def feed(self, text: str) -> tuple[str, str]:
        self._buf += text
        vis: list[str] = []
        think: list[str] = []
        while self._buf:
            if not self._lead_resolved and not self._in_think:
                # Leading limbo: hold everything until a tag tells us what it
                # was. Only a *full* tag resolves; otherwise keep buffering.
                i_open = self._buf.find(_THINK_OPEN)
                i_close = self._buf.find(_THINK_CLOSE)
                if i_open != -1 and (i_close == -1 or i_open < i_close):
                    vis.append(self._buf[:i_open])  # real visible before <think>
                    self._buf = self._buf[i_open + len(_THINK_OPEN):]
                    self._in_think = True
                    self._lead_resolved = True
                    continue
                if i_close != -1:
                    think.append(self._buf[:i_close])  # leaked leading reasoning
                    self._buf = self._buf[i_close + len(_THINK_CLOSE):]
                    self._lead_resolved = True
                    continue
                # No full tag yet — hold the whole buffer, emit nothing.
                break
            if not self._in_think:
                i_open = self._buf.find(_THINK_OPEN)
                i_close = self._buf.find(_THINK_CLOSE)
                # A `<think>` opener that comes before any close → enter think.
                if i_open != -1 and (i_close == -1 or i_open < i_close):
                    vis.append(self._buf[:i_open])
                    self._buf = self._buf[i_open + len(_THINK_OPEN):]
                    self._in_think = True
                    continue
                # A bare `</think>` with no opener (Kimi /coding et al. emit
                # reasoning first, then a lone close) → the text before it was
                # leaked reasoning, route it to the thinking channel and DROP
                # the literal tag so it never shows in the bubble. Mirrors the
                # non-streaming split_inline_think's leaked-close handling.
                if i_close != -1:
                    think.append(self._buf[:i_close])
                    self._buf = self._buf[i_close + len(_THINK_CLOSE):]
                    continue
                # no full tag — emit all but a possible partial-tag tail
                hold = _longest_partial_tag_suffix(self._buf)
                if hold:
                    vis.append(self._buf[:-hold])
                    self._buf = self._buf[-hold:]
                else:
                    vis.append(self._buf)
                    self._buf = ""
                break
            else:
                i = self._buf.find(_THINK_CLOSE)
                if i != -1:
                    think.append(self._buf[:i])
                    self._buf = self._buf[i + len(_THINK_CLOSE):]
                    self._in_think = False
                    continue
                hold = _longest_partial_tag_suffix(self._buf)
                if hold:
                    think.append(self._buf[:-hold])
                    self._buf = self._buf[-hold:]
                else:
                    think.append(self._buf)
                    self._buf = ""
                break
        return "".join(vis), "".join(think)

    def flush(self) -> tuple[str, str]:
        """Drain the tail. Anything still buffered while inside a think block
        is reasoning; otherwise it's visible (a dangling partial open tag is
        dropped as noise)."""
        tail = self._buf
        self._buf = ""
        if self._in_think:
            return "", tail
        # a leftover that's a partial open-tag prefix is an incomplete tag — drop it
        if tail and _THINK_OPEN.startswith(tail):
            return "", ""
        return tail, ""


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
