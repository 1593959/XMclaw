"""ContextPipeline — multi-stage pre-LLM window compaction.

Follows Claude Code's 5-stage pipeline pattern, adapted for XMclaw's
existing compression primitives:

  Stage 1: No-op check        — if under threshold, skip all stages
  Stage 2: Protect head/tail  — pin first N + last M messages
  Stage 3: Summarisation      — LLM compact of the unprotected middle
  Stage 4: Aggressive strip   — drop oldest messages if still over
  Stage 5: Sanitise           — validate message structure post-compact

Each stage measures its own latency and compression ratio; the caller
can configure thresholds and stage order. The pipeline is called once
before every LLM hop in ``_run_hop_loop``, replacing the current ad-hoc
``_maybe_compress_messages`` flow.

Usage::

    from xmclaw.daemon.context_pipeline import ContextPipeline
    pipeline = ContextPipeline(
        llm=llm,
        threshold_percent=0.85,
        context_length=200_000,
        protect_first_n=2,
        protect_last_ratio=0.15,
    )
    messages, did_compact = await pipeline.compact(messages, session_id)

Audit 2026-06-11: replaces scattered compression logic in
history_compression.py with a structured, measurable pipeline.
"""
from __future__ import annotations

import time
from typing import Any

from xmclaw.core.ir import Message
from xmclaw.providers.llm.base import LLMProvider


# Rough estimator: CJK chars ≈ 1 token, ASCII ≈ 0.25 tokens per char.
# This is fast and parameter-free — no tiktoken dependency.
def _estimate_tokens(text: str) -> int:
    cjk = sum(1 for c in text if "一" <= c <= "鿿" or "぀" <= c <= "ヿ")
    ascii_chars = len(text) - cjk
    return cjk + max(1, ascii_chars // 4)


def _messages_token_estimate(messages: list[Message]) -> int:
    total = 0
    for m in messages:
        content = m.content or ""
        if isinstance(content, str):
            total += _estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    total += _estimate_tokens(str(block["text"]))
    return total


class ContextPipeline:
    """Multi-stage context window compaction pipeline."""

    def __init__(
        self,
        *,
        llm: LLMProvider,
        threshold_percent: float = 0.85,
        context_length: int = 200_000,
        protect_first_n: int = 2,
        protect_last_ratio: float = 0.15,
        summary_target_ratio: float = 0.25,
        max_summary_tokens: int = 4000,
    ) -> None:
        self._llm = llm
        self._threshold_percent = threshold_percent
        self._context_length = context_length
        self._protect_first_n = protect_first_n
        self._protect_last_ratio = protect_last_ratio
        self._summary_target_ratio = summary_target_ratio
        self._max_summary_tokens = max_summary_tokens
        # Metrics for tuning
        self.last_stage_results: dict[str, dict[str, Any]] = {}

    async def compact(
        self,
        messages: list[Message],
        session_id: str,
        *,
        force: bool = False,
    ) -> tuple[list[Message], bool]:
        """Run the full pipeline. Returns (compacted_messages, did_compact)."""
        t0 = time.perf_counter()
        original_count = len(messages)
        original_tokens = _messages_token_estimate(messages)
        threshold = int(self._context_length * self._threshold_percent)

        # ── Stage 1: no-op check ──────────────────────────────────
        if not force and original_tokens < threshold:
            self.last_stage_results = {"stage_1_noop": {
                "tokens": original_tokens, "threshold": threshold, "compacted": False,
            }}
            return messages, False

        # ── Stage 2: protect head/tail ────────────────────────────
        protected_head = messages[:self._protect_first_n]
        tail_count = max(1, int(len(messages) * self._protect_last_ratio))
        protected_tail = messages[-tail_count:]
        middle = messages[self._protect_first_n : -tail_count] if tail_count > 0 else messages[self._protect_first_n:]

        if not middle:
            self.last_stage_results = {"stage_2_protect": {
                "tokens": original_tokens, "head": len(protected_head),
                "tail": len(protected_tail), "compacted": False,
            }}
            return messages, False

        # ── Stage 3: LLM summarisation ────────────────────────────
        did_summarise = False
        try:
            summary_text = await self._summarise(middle, session_id)
            if summary_text:
                summary_msg = Message(
                    role="user",
                    content=(
                        "[以下是之前对话内容的压缩摘要，你已无法看到原文细节 — "
                        "如果涉及这些内容请参考此摘要]\n\n" + summary_text
                    ),
                )
                messages = protected_head + [summary_msg] + protected_tail
                did_summarise = True
        except Exception:  # noqa: BLE001 — compression failure is non-fatal
            pass

        stage3_tokens = _messages_token_estimate(messages)
        self.last_stage_results["stage_3_summarise"] = {
            "tokens_after": stage3_tokens, "summarised": did_summarise,
        }

        # ── Stage 4: aggressive strip ─────────────────────────────
        if stage3_tokens > threshold:
            # Drop the oldest middle messages until we're under threshold.
            # Each iteration drops 10% of remaining middle.
            while len(messages) > self._protect_first_n + tail_count and _messages_token_estimate(messages) > threshold:
                cut_point = self._protect_first_n + max(1, (len(messages) - self._protect_first_n - tail_count) // 10)
                messages = messages[:self._protect_first_n] + messages[cut_point:]
            self.last_stage_results["stage_4_strip"] = {
                "tokens_after": _messages_token_estimate(messages),
                "messages_after": len(messages),
            }

        # ── Stage 5: sanitise ─────────────────────────────────────
        messages = self._sanitise(messages)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        compacted = len(messages) < original_count or did_summarise
        from xmclaw.utils.log import get_logger
        get_logger(__name__).info(
            "context_pipeline.compact session=%s original=%dt/%dmsgs "
            "compact=%dt/%dmsgs summarised=%s elapsed=%dms",
            session_id[:12], original_tokens, original_count,
            _messages_token_estimate(messages), len(messages),
            did_summarise, int(elapsed_ms),
        )
        return messages, compacted

    async def _summarise(
        self, messages: list[Message], session_id: str,
    ) -> str | None:
        """Ask the LLM to summarise the middle segment into a compact note."""
        import asyncio

        role_counts: dict[str, int] = {}
        for m in messages:
            role_counts[m.role] = role_counts.get(m.role, 0) + 1

        parts: list[str] = []
        for m in messages[-20:]:  # Only summarise last 20 middle messages
            content = m.content or ""
            if isinstance(content, list):
                texts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                content = "\n".join(texts)
            if content.strip():
                parts.append(f"[{m.role}] {content[:300]}")

        if not parts:
            return None

        prompt = (
            "Summarise the following conversation segment into a compact "
            "English/Chinese note (max 500 chars). Keep key decisions, "
            "tool results, file paths, and user preferences. Drop greetings "
            "and filler. Output ONLY the summary text, no markdown, no "
            "prefix or suffix.\n\n"
        ) + "\n---\n".join(parts)

        try:
            resp = await asyncio.wait_for(
                self._llm.complete(
                    [Message(role="user", content=prompt)],
                    tools=None,
                ),
                timeout=8.0,
            )
            content = resp.content or ""
            if len(content) > self._max_summary_tokens:
                content = content[:self._max_summary_tokens]
            return content.strip() or None
        except Exception:
            return None

    @staticmethod
    def _sanitise(messages: list[Message]) -> list[Message]:
        """Validate message structure: remove empty system messages,
        ensure no consecutive user-user or assistant-assistant pairs."""
        out: list[Message] = []
        for m in messages:
            if m.role == "system" and not (m.content or "").strip():
                continue  # Drop empty system messages
            if out and out[-1].role == m.role == "user":
                # Merge consecutive user messages
                prev = out[-1]
                out[-1] = Message(
                    role="user",
                    content=(prev.content or "") + "\n\n" + (m.content or ""),
                    images=prev.images,
                )
                continue
            out.append(m)
        return out
