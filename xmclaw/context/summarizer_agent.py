"""LLM-backed summarizer agent for context compaction."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from xmclaw.providers.llm.base import Message
from xmclaw.utils.redact import redact_string


@dataclass(slots=True)
class SummarizerAgent:
    """Small adapter that turns an LLM provider into a compressor callable."""

    llm: Any
    timeout_s: float = 60.0

    async def summarize(self, prompt: str, max_tokens: int) -> str | None:
        complete = getattr(self.llm, "complete", None)
        if not callable(complete):
            return None
        try:
            resp = await asyncio.wait_for(
                complete(
                    [
                        Message(
                            role="system",
                            content=(
                                "You are XMclaw's summarizer agent. Return only "
                                "the requested handoff summary body."
                            ),
                        ),
                        Message(role="user", content=prompt),
                    ],
                    tools=None,
                ),
                timeout=max(1.0, float(self.timeout_s)),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return None
        text = str(getattr(resp, "content", resp) or "").strip()
        if not text:
            return None
        return redact_string(text)


__all__ = ["SummarizerAgent"]
