"""AwaySummary — short recap for "while you were away".

Wave-32+ (2026-05-18). Ports the free-code-main ``services/
awaySummary.ts`` pattern.

Use case
========

The user steps away from the chat (closes the tab, lets the daemon
run overnight, comes back next morning). When they re-open, instead
of having to re-read the whole transcript they get a 1–3 sentence
recap of what the session was working on and what the next step is.

How XMclaw uses it
==================

A REST endpoint :http:get:`/api/v2/session/{session_id}/recap` calls
:func:`generate_away_summary` on demand. The frontend can hit this
when the chat panel reopens after a gap. The summary is generated
fresh each call (no caching) — the function is cheap enough
(small-fast-model, ≤30 messages, no tools) that caching adds more
complexity than it saves.

The function is also reusable from anywhere — e.g. a CLI ``xmclaw
recap <session_id>`` could call it directly without touching the
HTTP surface.

Design notes
============

* **Truncates to the last 30 messages.** Large sessions otherwise
  blow past the small-fast-model's context. 30 ≈ 15 user/assistant
  exchanges, plenty for "where we left off."
* **No tools advertised** — purely text-in / text-out. The summary
  generator must not be able to act, only describe.
* **Returns ``None``** on empty transcript, abort, or LLM error.
  Caller decides whether to surface a placeholder ("nothing to
  recap yet") or hide the card entirely.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from xmclaw.utils.log import get_logger

if TYPE_CHECKING:
    from xmclaw.core.ir import Message
    from xmclaw.providers.llm.base import LLMProvider

logger = get_logger(__name__)


_RECENT_MESSAGE_WINDOW = 30

_PROMPT = (
    "The user stepped away from this chat and is coming back. Write "
    "exactly 1-3 short sentences as a recap. Start by stating the "
    "high-level task they were working on (what they are building or "
    "debugging — not implementation details). Next: the concrete "
    "next step they should take. Skip status reports, commit recaps, "
    "and verbose summaries. Plain prose, no headings, no bullets."
)


async def generate_away_summary(
    history: list["Message"],
    llm: "LLMProvider",
    *,
    max_messages: int = _RECENT_MESSAGE_WINDOW,
    timeout_s: float = 30.0,
) -> str | None:
    """Produce a "while you were away" recap from a session history.

    Parameters
    ----------
    history
        The session's message list as kept by AgentLoop. The system
        prompt is automatically excluded — only user / assistant /
        tool messages contribute.
    llm
        LLMProvider used for the one-shot completion. Caller decides
        whether to use the agent's main LLM or a small-fast model.
    max_messages
        Truncate to this many trailing messages before prompting.
        Default 30; cap > 0 — 0 or negative returns ``None``.
    timeout_s
        Hard timeout for the LLM call. On exceed returns ``None``;
        a stuck recap shouldn't block the user's next interaction.

    Returns
    -------
    str | None
        The recap text (stripped), or ``None`` on empty history,
        timeout, or LLM error.
    """
    if not history or max_messages <= 0:
        return None

    # Lazy imports — keeps cognition/ from importing providers/ at
    # module load (DAG check enforces this direction).
    from xmclaw.core.ir import Message

    # Exclude system messages, keep last N.
    nonsystem = [m for m in history if getattr(m, "role", None) != "system"]
    if not nonsystem:
        return None
    tail = nonsystem[-max_messages:]
    prompt_msgs: list[Message] = list(tail) + [
        Message(role="user", content=_PROMPT),
    ]

    try:
        resp = await asyncio.wait_for(
            llm.complete(prompt_msgs, tools=None),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        logger.warning("away_summary.timeout window=%d", max_messages)
        return None
    except Exception as exc:  # noqa: BLE001 — recap must never block UX
        logger.warning("away_summary.failed err=%s", exc)
        return None

    text = (getattr(resp, "content", None) or "").strip()
    return text or None


__all__ = ["generate_away_summary"]
