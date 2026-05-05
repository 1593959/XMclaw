"""P0-1: ContextCompressor — full Hermes port (5-phase pipeline).

Adapted from ``hermes-agent/agent/context_compressor.py`` (1230 LOC).
The XMclaw version drops Hermes-specific dependencies (auxiliary_client,
ContextEngine ABC, model_metadata.get_model_context_length) and binds
to XMclaw's ``Message`` / ``ToolCall`` dataclasses. The algorithm is
unchanged.

Pipeline:

  1. **Prune old tool results** (cheap, no LLM call) — delegated to
     ``xmclaw.context.tool_result_prune.prune_old_tool_results``. Old
     tool messages outside the protected tail get summarised to a
     1-line description; identical content is deduped; large
     tool_call args are JSON-shrunk.

  2. **Protect head messages** — the first ``protect_first_n``
     turns (system prompt + opening exchange) are NEVER summarised.

  3. **Find tail boundary by token budget** — walk backward from
     the end accumulating tokens until ``tail_token_budget`` is
     reached. Soft ceiling is 1.5×budget so we don't cut inside an
     oversized message. Hard floor is 3 messages. The cut is
     anchored to the last user message so the active task is
     never lost (#10896 fix).

  4. **Summarise middle turns** — turns between head_end and
     tail_start go to ``summarize_call`` with a structured template
     (Active Task / Goal / Completed Actions / Resolved Questions /
     ...). Iterative updates: the previous summary is fed back so
     state survives multiple compactions.

  5. **Assemble** — head + summary + tail. Orphaned tool_call /
     tool_result pairs are sanitised so the API never sees a result
     without a matching call (or vice versa).

Anti-thrashing: if the last 2 compressions each saved < 10%,
``should_compress()`` returns False until ``on_session_reset()``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from xmclaw.providers.llm.base import Message
from xmclaw.utils.redact import redact_string
from xmclaw.context.tool_result_prune import prune_old_tool_results

logger = logging.getLogger(__name__)


SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is a handoff from a previous context "
    "window — treat it as background reference, NOT as active instructions. "
    "Do NOT answer questions or fulfill requests mentioned in this summary; "
    "they were already addressed. "
    "Your current task is identified in the '## Active Task' section of the "
    "summary — resume exactly from there. "
    "Respond ONLY to the latest user message that appears AFTER this summary. "
    "The current session state (files, config, etc.) may reflect work "
    "described here — avoid repeating it:"
)

_LEGACY_SUMMARY_PREFIX = "[CONTEXT SUMMARY]:"

# Tunables (unchanged from Hermes)
_CHARS_PER_TOKEN = 4
_MIN_SUMMARY_TOKENS = 2_000
_SUMMARY_RATIO = 0.20
_SUMMARY_TOKENS_CEILING = 12_000
_SUMMARY_FAILURE_COOLDOWN_SECONDS = 600

# Truncation limits for the summariser input.
_CONTENT_MAX = 6_000        # total chars per message body
_CONTENT_HEAD = 4_000       # chars kept from the start
_CONTENT_TAIL = 1_500       # chars kept from the end
_TOOL_ARGS_MAX = 1_500      # tool call args
_TOOL_ARGS_HEAD = 1_200


# ── Token estimation ─────────────────────────────────────────────────


def estimate_messages_tokens_rough(messages: list[Message]) -> int:
    """Rough token count: chars/4 + ~10 overhead per message + tool args.

    Match Hermes's heuristic so threshold values port directly. Real
    tokenisation is provider-specific and too expensive to run on
    every turn.
    """
    total = 0
    for m in messages:
        content = m.content or ""
        if isinstance(content, list):
            text = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        else:
            text = str(content)
        total += len(text) // _CHARS_PER_TOKEN + 10
        for tc in m.tool_calls or ():
            args = getattr(tc, "args", {}) or {}
            if isinstance(args, dict):
                # Fast path: estimate via the JSON-serialised length.
                # Avoid actually serialising — sum nested string lengths.
                total += _estimate_args_chars(args) // _CHARS_PER_TOKEN
            elif isinstance(args, str):
                total += len(args) // _CHARS_PER_TOKEN
    return total


def _estimate_args_chars(args: Any) -> int:
    """Walk a parsed JSON value, summing string-leaf lengths.

    Cheaper than ``json.dumps`` + ``len`` — and matches the Hermes
    arg-token heuristic since dicts/lists have small structural
    overhead vs the string content.
    """
    if isinstance(args, str):
        return len(args)
    if isinstance(args, dict):
        return sum(_estimate_args_chars(v) for v in args.values())
    if isinstance(args, list):
        return sum(_estimate_args_chars(v) for v in args)
    return 0


# ── Per-session state ────────────────────────────────────────────────


@dataclasses.dataclass
class _SessionState:
    """Compaction state per session_id (anti-thrashing + iterative summary)."""

    previous_summary: Optional[str] = None
    last_savings_pct: float = 100.0
    ineffective_count: int = 0
    failure_cooldown_until: float = 0.0


# ── Compressor ───────────────────────────────────────────────────────


class ContextCompressor:
    """5-phase context compressor — see module docstring for algorithm.

    Args:
        model: model name for logging only (not used for tokenisation).
        summarize_call: async callable ``(prompt: str, max_tokens: int)
            -> str | None``. Returns the summary text or None on failure.
            XMclaw wires this to the agent's main LLM provider; pass
            a dedicated cheap-model client if you want a different
            summary backend.
        threshold_percent: fraction of context_length at which compression
            fires. Defaults to 0.50 (50%).
        protect_first_n: number of leading messages NEVER summarised
            (system prompt + opening exchange).
        protect_last_n: minimum tail message count (token-budget overrides
            this — see ``_find_tail_cut_by_tokens``).
        summary_target_ratio: target summary length as fraction of the
            threshold (0.20 = summary is 20% of threshold). Clamped
            to [0.10, 0.80].
        context_length: model's full context window in tokens. Defaults
            to 200_000 (Anthropic Sonnet baseline). Set per-model when
            wiring up.
        quiet_mode: when True, suppress info-level logs (probe / test
            harness use).
    """

    def __init__(
        self,
        model: str,
        summarize_call: Callable[[str, int], Awaitable[Optional[str]]],
        *,
        threshold_percent: float = 0.50,
        protect_first_n: int = 3,
        protect_last_n: int = 20,
        summary_target_ratio: float = 0.20,
        context_length: int = 200_000,
        quiet_mode: bool = False,
    ) -> None:
        self.model = model
        self.summarize_call = summarize_call
        self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n
        self.summary_target_ratio = max(0.10, min(summary_target_ratio, 0.80))
        self.quiet_mode = quiet_mode

        self.context_length = max(int(context_length), 4_096)
        self.threshold_percent = threshold_percent
        self.threshold_tokens = max(int(self.context_length * threshold_percent), 4_096)
        self.tail_token_budget = int(self.threshold_tokens * self.summary_target_ratio)
        self.max_summary_tokens = min(
            int(self.context_length * 0.05), _SUMMARY_TOKENS_CEILING,
        )
        self.compression_count = 0

        self._states: dict[str, _SessionState] = {}

        if not quiet_mode:
            logger.info(
                "ContextCompressor: model=%s ctx=%d threshold=%d (%.0f%%) "
                "tail_budget=%d",
                model, self.context_length, self.threshold_tokens,
                threshold_percent * 100, self.tail_token_budget,
            )

    # ── Per-session API ──────────────────────────────────────────────

    def _state(self, session_id: str) -> _SessionState:
        st = self._states.get(session_id)
        if st is None:
            st = _SessionState()
            self._states[session_id] = st
        return st

    def on_session_reset(self, session_id: str = "") -> None:
        """Drop all per-session compaction state for ``session_id``."""
        self._states.pop(session_id, None)

    def should_compress(
        self, prompt_tokens: int, *, session_id: str = "",
    ) -> bool:
        """Decide whether compression should fire for this turn.

        Returns False when:
          * ``prompt_tokens < threshold_tokens`` — no need yet
          * Last 2 compressions saved < 10% — anti-thrashing back-off
        """
        if prompt_tokens < self.threshold_tokens:
            return False
        st = self._state(session_id)
        if st.ineffective_count >= 2:
            if not self.quiet_mode:
                logger.warning(
                    "ContextCompressor.skip session=%s — last %d "
                    "compactions saved <10%% each. Consider /new "
                    "or /compress <topic>.",
                    session_id or "?", st.ineffective_count,
                )
            return False
        return True

    # ── Main entry point ────────────────────────────────────────────

    async def compress(
        self,
        messages: list[Message],
        *,
        session_id: str = "",
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
    ) -> list[Message]:
        """Run the 5-phase pipeline. Returns a new ``list[Message]``.

        Args:
            messages: full conversation including system prompt.
            session_id: anchor for per-session state (anti-thrashing,
                iterative summary). Empty string means "no per-session
                state".
            current_tokens: optional pre-computed token estimate. Falls
                back to ``estimate_messages_tokens_rough(messages)``.
            focus_topic: optional priority hint passed to the summariser.
                When set, the summariser allocates 60-70% of the budget
                to content related to this topic.

        On any internal error the original messages are returned
        unchanged — context compression NEVER fails a user turn.
        """
        n = len(messages)
        min_for_compress = self.protect_first_n + 3 + 1
        if n <= min_for_compress:
            if not self.quiet_mode:
                logger.warning(
                    "ContextCompressor.skip: only %d msgs (need > %d)",
                    n, min_for_compress,
                )
            return list(messages)

        display_tokens = current_tokens or estimate_messages_tokens_rough(messages)
        st = self._state(session_id)

        try:
            # Phase 1: prune (cheap, no LLM call).
            messages, pruned = prune_old_tool_results(
                list(messages),
                protect_tail_tokens=self.tail_token_budget,
                protect_tail_count_floor=self.protect_last_n,
            )
            if pruned and not self.quiet_mode:
                logger.info(
                    "ContextCompressor.prune session=%s pruned=%d",
                    session_id or "?", pruned,
                )

            # Phase 2: head boundary.
            compress_start = self.protect_first_n
            compress_start = self._align_boundary_forward(messages, compress_start)

            # Phase 3: tail boundary by token budget.
            compress_end = self._find_tail_cut_by_tokens(messages, compress_start)
            if compress_start >= compress_end:
                return messages

            turns_to_summarize = messages[compress_start:compress_end]

            if not self.quiet_mode:
                logger.info(
                    "ContextCompressor.fire session=%s tokens=%d threshold=%d "
                    "summarising turns %d-%d (%d turns), head=%d tail=%d",
                    session_id or "?", display_tokens, self.threshold_tokens,
                    compress_start + 1, compress_end, len(turns_to_summarize),
                    compress_start, n - compress_end,
                )

            # Phase 4: summarise.
            summary = await self._generate_summary(
                turns_to_summarize, st, focus_topic=focus_topic,
            )

            # Phase 5: assemble.
            compressed = self._assemble_compressed(
                messages, compress_start, compress_end, summary,
            )
            compressed = self._sanitize_tool_pairs(compressed)

            # Update anti-thrashing state.
            new_estimate = estimate_messages_tokens_rough(compressed)
            saved = display_tokens - new_estimate
            savings_pct = (saved / display_tokens * 100) if display_tokens > 0 else 0
            st.last_savings_pct = savings_pct
            if savings_pct < 10:
                st.ineffective_count += 1
            else:
                st.ineffective_count = 0

            self.compression_count += 1
            if not self.quiet_mode:
                logger.info(
                    "ContextCompressor.done session=%s %d→%d msgs "
                    "(~%d tokens saved, %.0f%%)",
                    session_id or "?", n, len(compressed), saved, savings_pct,
                )

            return compressed
        except Exception as exc:  # noqa: BLE001 — never fail a turn
            if not self.quiet_mode:
                logger.warning(
                    "ContextCompressor.failed session=%s err=%s — returning original",
                    session_id or "?", exc,
                )
            return list(messages)

    # ── Phase 2-3: boundary helpers ─────────────────────────────────

    @staticmethod
    def _align_boundary_forward(messages: list[Message], idx: int) -> int:
        """Push a compress-start boundary past orphan tool results.

        If ``messages[idx]`` is a tool result (no parent assistant in
        the head), slide forward until we hit a non-tool message so
        the summarised region doesn't start mid-group.
        """
        while idx < len(messages) and messages[idx].role == "tool":
            idx += 1
        return idx

    @staticmethod
    def _align_boundary_backward(messages: list[Message], idx: int) -> int:
        """Pull a compress-end boundary back to avoid splitting a
        tool_call / result group.

        If ``idx`` lands in the middle of a result group (consecutive
        tool messages preceded by an assistant with tool_calls), walk
        back past them to include the parent assistant in the
        summarised region.
        """
        if idx <= 0 or idx >= len(messages):
            return idx
        check = idx - 1
        while check >= 0 and messages[check].role == "tool":
            check -= 1
        if (check >= 0 and messages[check].role == "assistant"
                and messages[check].tool_calls):
            idx = check
        return idx

    @staticmethod
    def _find_last_user_message_idx(
        messages: list[Message], head_end: int,
    ) -> int:
        for i in range(len(messages) - 1, head_end - 1, -1):
            if messages[i].role == "user":
                return i
        return -1

    def _ensure_last_user_message_in_tail(
        self, messages: list[Message], cut_idx: int, head_end: int,
    ) -> int:
        """Anchor the tail cut so the most recent user message survives.

        Without this, ``_align_boundary_backward`` can pull the cut past
        a user message — the summariser then writes it into "Pending User
        Asks", but ``SUMMARY_PREFIX`` tells the next model to respond
        only to user messages AFTER the summary, so the active task
        silently disappears (Hermes #10896).
        """
        last_user = self._find_last_user_message_idx(messages, head_end)
        if last_user < 0 or last_user >= cut_idx:
            return cut_idx
        if not self.quiet_mode:
            logger.debug(
                "ContextCompressor.anchor cut=%d → %d to keep last user msg",
                cut_idx, last_user,
            )
        return max(last_user, head_end + 1)

    def _find_tail_cut_by_tokens(
        self, messages: list[Message], head_end: int,
    ) -> int:
        """Walk backward accumulating tokens until ``tail_token_budget``
        is reached. Returns the index where the protected tail starts.
        """
        budget = self.tail_token_budget
        n = len(messages)
        min_tail = min(3, n - head_end - 1) if n - head_end > 1 else 0
        soft_ceiling = int(budget * 1.5)
        accumulated = 0
        cut_idx = n

        for i in range(n - 1, head_end - 1, -1):
            m = messages[i]
            content = m.content or ""
            if isinstance(content, list):
                content_len = sum(
                    len(p.get("text", "")) for p in content if isinstance(p, dict)
                )
            else:
                content_len = len(content)
            msg_tokens = content_len // _CHARS_PER_TOKEN + 10
            for tc in m.tool_calls or ():
                msg_tokens += _estimate_args_chars(getattr(tc, "args", {}) or {}) // _CHARS_PER_TOKEN

            if accumulated + msg_tokens > soft_ceiling and (n - i) >= min_tail:
                break
            accumulated += msg_tokens
            cut_idx = i

        fallback_cut = n - min_tail
        if cut_idx > fallback_cut:
            cut_idx = fallback_cut
        if cut_idx <= head_end:
            cut_idx = max(fallback_cut, head_end + 1)

        cut_idx = self._align_boundary_backward(messages, cut_idx)
        cut_idx = self._ensure_last_user_message_in_tail(messages, cut_idx, head_end)
        return max(cut_idx, head_end + 1)

    # ── Phase 4: summary generation ────────────────────────────────

    def _compute_summary_budget(self, turns: list[Message]) -> int:
        """Scale the summary token budget to the content size."""
        content_tokens = estimate_messages_tokens_rough(turns)
        budget = int(content_tokens * _SUMMARY_RATIO)
        return max(_MIN_SUMMARY_TOKENS, min(budget, self.max_summary_tokens))

    def _serialize_for_summary(self, turns: list[Message]) -> str:
        """Serialise turns into labeled text for the summariser.

        Includes tool call arguments + result content (up to
        ``_CONTENT_MAX`` chars per message) so the summariser can
        preserve specific details. All content is redacted before
        serialisation to prevent secrets from reaching the auxiliary
        model.
        """
        import json as _json

        parts: list[str] = []
        for m in turns:
            role = m.role or "unknown"
            raw_content = m.content or ""
            if isinstance(raw_content, list):
                raw_content = "".join(
                    p.get("text", "") for p in raw_content if isinstance(p, dict)
                )
            content = redact_string(str(raw_content))

            if role == "tool":
                tool_id = m.tool_call_id or ""
                if len(content) > _CONTENT_MAX:
                    content = (
                        content[:_CONTENT_HEAD]
                        + "\n...[truncated]...\n"
                        + content[-_CONTENT_TAIL:]
                    )
                parts.append(f"[TOOL RESULT {tool_id}]: {content}")
                continue

            if role == "assistant":
                if len(content) > _CONTENT_MAX:
                    content = (
                        content[:_CONTENT_HEAD]
                        + "\n...[truncated]...\n"
                        + content[-_CONTENT_TAIL:]
                    )
                if m.tool_calls:
                    tc_parts: list[str] = []
                    for tc in m.tool_calls:
                        name = getattr(tc, "name", "?")
                        args = getattr(tc, "args", {}) or {}
                        try:
                            args_str = _json.dumps(args, ensure_ascii=False)
                        except (TypeError, ValueError):
                            args_str = str(args)
                        args_str = redact_string(args_str)
                        if len(args_str) > _TOOL_ARGS_MAX:
                            args_str = args_str[:_TOOL_ARGS_HEAD] + "..."
                        tc_parts.append(f"  {name}({args_str})")
                    content += "\n[Tool calls:\n" + "\n".join(tc_parts) + "\n]"
                parts.append(f"[ASSISTANT]: {content}")
                continue

            if len(content) > _CONTENT_MAX:
                content = (
                    content[:_CONTENT_HEAD]
                    + "\n...[truncated]...\n"
                    + content[-_CONTENT_TAIL:]
                )
            parts.append(f"[{role.upper()}]: {content}")

        return "\n\n".join(parts)

    async def _generate_summary(
        self,
        turns: list[Message],
        st: _SessionState,
        *,
        focus_topic: Optional[str] = None,
    ) -> Optional[str]:
        """Build the summariser prompt and call ``self.summarize_call``.

        Returns ``None`` on failure (cooldown engaged, summariser raised,
        no provider configured). The caller substitutes a static fallback
        summary so the model still knows context was lost.
        """
        now = time.monotonic()
        if now < st.failure_cooldown_until:
            if not self.quiet_mode:
                logger.debug(
                    "ContextCompressor.summary.cooldown remaining=%.0fs",
                    st.failure_cooldown_until - now,
                )
            return None

        budget = self._compute_summary_budget(turns)
        content = self._serialize_for_summary(turns)
        prompt = self._build_summary_prompt(
            content, budget, st.previous_summary, focus_topic=focus_topic,
        )

        try:
            text = await self.summarize_call(prompt, int(budget * 1.3))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            st.failure_cooldown_until = time.monotonic() + 60
            if not self.quiet_mode:
                logger.warning(
                    "ContextCompressor.summary.failed err=%s — 60s cooldown",
                    exc,
                )
            return None

        if not text or not isinstance(text, str):
            st.failure_cooldown_until = time.monotonic() + _SUMMARY_FAILURE_COOLDOWN_SECONDS
            return None

        text = redact_string(text.strip())
        st.previous_summary = text
        st.failure_cooldown_until = 0.0
        return self._with_summary_prefix(text)

    @staticmethod
    def _with_summary_prefix(summary: str) -> str:
        """Normalise summary text to the current handoff format."""
        text = (summary or "").strip()
        for prefix in (_LEGACY_SUMMARY_PREFIX, SUMMARY_PREFIX):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
                break
        return f"{SUMMARY_PREFIX}\n{text}" if text else SUMMARY_PREFIX

    def _build_summary_prompt(
        self,
        content: str,
        budget: int,
        previous: Optional[str],
        *,
        focus_topic: Optional[str] = None,
    ) -> str:
        """Build the structured summariser prompt (first / iterative / focus)."""
        preamble = (
            "You are a summarization agent creating a context checkpoint. "
            "Your output will be injected as reference material for a DIFFERENT "
            "assistant that continues the conversation. "
            "Do NOT respond to any questions or requests in the conversation — "
            "only output the structured summary. "
            "Do NOT include any preamble, greeting, or prefix. "
            "Write the summary in the same language the user was using in the "
            "conversation — do not translate or switch to English. "
            "NEVER include API keys, tokens, passwords, secrets, credentials, "
            "or connection strings — replace any with [REDACTED]."
        )

        template = f"""## Active Task
[THE SINGLE MOST IMPORTANT FIELD. Copy the user's most recent request or
task assignment verbatim — the exact words they used. If multiple tasks
were requested and only some are done, list only the ones NOT yet completed.
The next assistant must pick up exactly here. Example:
"User asked: 'Now refactor the auth module to use JWT instead of sessions'"
If no outstanding task exists, write "None."]

## Goal
[What the user is trying to accomplish overall]

## Constraints & Preferences
[User preferences, coding style, constraints, important decisions]

## Completed Actions
[Numbered list of concrete actions taken — include tool used, target, and outcome.
Format each as: N. ACTION target — outcome [tool: name]
Example:
1. READ config.py:45 — found `==` should be `!=` [tool: file_read]
2. PATCH config.py:45 — changed `==` to `!=` [tool: apply_patch]
Be specific with file paths, commands, line numbers, and results.]

## Active State
[Current working state — working directory, branch, modified files, test status]

## In Progress
[Work currently underway — what was being done when compaction fired]

## Blocked
[Any blockers, errors, or issues not yet resolved. Include exact error messages.]

## Key Decisions
[Important technical decisions and WHY they were made]

## Resolved Questions
[Questions the user asked that were ALREADY answered — include the answer]

## Pending User Asks
[Questions/requests from the user NOT yet answered. If none, write "None."]

## Relevant Files
[Files read, modified, or created — with brief note on each]

## Remaining Work
[What remains to be done — framed as context, not instructions]

## Critical Context
[Specific values, error messages, configuration details, or data that would
be lost without explicit preservation. NEVER include API keys, tokens, or
credentials — write [REDACTED] instead.]

Target ~{budget} tokens. Be CONCRETE — include file paths, command outputs,
error messages, line numbers, and specific values. Avoid vague descriptions
like "made some changes" — say exactly what changed.

Write only the summary body. Do not include any preamble or prefix."""

        if previous:
            prompt = f"""{preamble}

You are updating a context compaction summary. A previous compaction produced
the summary below. New conversation turns have occurred since then and need to
be incorporated.

PREVIOUS SUMMARY:
{previous}

NEW TURNS TO INCORPORATE:
{content}

Update the summary using this exact structure. PRESERVE all existing
information that is still relevant. ADD new completed actions to the numbered
list (continue numbering). Move items from "In Progress" to "Completed
Actions" when done. Move answered questions to "Resolved Questions". Update
"Active State" to reflect current state. Remove information only if it is
clearly obsolete. CRITICAL: Update "## Active Task" to reflect the user's most
recent unfulfilled request — this is the most important field for task
continuity.

{template}"""
        else:
            prompt = f"""{preamble}

Create a structured handoff summary for a different assistant that will
continue this conversation after earlier turns are compacted. The next
assistant should be able to understand what happened without re-reading
the original turns.

TURNS TO SUMMARIZE:
{content}

Use this exact structure:

{template}"""

        if focus_topic:
            prompt += (
                f'\n\nFOCUS TOPIC: "{focus_topic}"\n'
                f'The user has requested that this compaction PRIORITISE '
                f'preserving all information related to the focus topic above. '
                f'For content related to "{focus_topic}", include full detail '
                f'— exact values, file paths, command outputs, error messages, '
                f'and decisions. For content NOT related to the focus topic, '
                f'summarise more aggressively. The focus topic sections '
                f'should receive roughly 60-70% of the budget. Even for the '
                f'focus topic, NEVER preserve API keys, tokens, passwords, '
                f'or credentials — use [REDACTED].'
            )

        return prompt

    # ── Phase 5: assembly + sanitisation ───────────────────────────

    def _assemble_compressed(
        self,
        messages: list[Message],
        compress_start: int,
        compress_end: int,
        summary: Optional[str],
    ) -> list[Message]:
        """Build head + summary + tail, picking summary role to avoid
        consecutive same-role messages on either side.
        """
        n = len(messages)
        compressed: list[Message] = []

        # Head — append a "compaction note" to the system prompt.
        for i in range(compress_start):
            m = messages[i]
            if i == 0 and m.role == "system":
                existing = m.content or ""
                note = (
                    "[Note: Some earlier conversation turns have been "
                    "compacted into a handoff summary to preserve context "
                    "space. The current session state may still reflect "
                    "earlier work, so build on that summary and state "
                    "rather than re-doing work.]"
                )
                if note not in existing:
                    m = dataclasses.replace(m, content=existing + "\n\n" + note)
            compressed.append(m)

        # Static fallback if summary failed.
        if not summary:
            n_dropped = compress_end - compress_start
            summary = (
                f"{SUMMARY_PREFIX}\n"
                f"Summary generation was unavailable. {n_dropped} conversation "
                f"turns were removed to free context space but could not be "
                f"summarized. Continue based on the recent messages below "
                f"and the current state of any files or resources."
            )

        # Pick a summary role that doesn't collide with adjacent messages.
        last_head_role = (
            messages[compress_start - 1].role if compress_start > 0 else "user"
        )
        first_tail_role = (
            messages[compress_end].role if compress_end < n else "user"
        )

        if last_head_role in ("assistant", "tool"):
            summary_role = "user"
        else:
            summary_role = "assistant"

        merge_into_tail = False
        if summary_role == first_tail_role:
            flipped = "assistant" if summary_role == "user" else "user"
            if flipped != last_head_role:
                summary_role = flipped
            else:
                # Head=assistant + tail=user (or vice versa) — neither role
                # works as a standalone insert. Merge into the first tail
                # message instead.
                merge_into_tail = True

        if not merge_into_tail:
            compressed.append(Message(role=summary_role, content=summary))

        # Tail — merge summary into first tail message if needed.
        for i in range(compress_end, n):
            m = messages[i]
            if merge_into_tail and i == compress_end:
                original = m.content or ""
                m = dataclasses.replace(
                    m,
                    content=(
                        summary
                        + "\n\n--- END OF CONTEXT SUMMARY — "
                        "respond to the message below, not the summary above ---\n\n"
                        + str(original)
                    ),
                )
                merge_into_tail = False
            compressed.append(m)

        return compressed

    def _sanitize_tool_pairs(self, messages: list[Message]) -> list[Message]:
        """Fix orphaned tool_call / tool_result pairs after compression.

        Two failure modes:
          1. A tool result references a call_id whose assistant tool_call
             was removed. Anthropic / OpenAI both reject this with
             "no tool call found for ...".
          2. An assistant message has tool_calls whose results were
             dropped. The API rejects this too — every tool_call must be
             followed by a result with the matching id.

        Removes orphaned results, inserts stub results for orphaned calls.
        """
        surviving_call_ids: set[str] = set()
        for m in messages:
            if m.role == "assistant":
                for tc in m.tool_calls or ():
                    cid = getattr(tc, "id", "") or ""
                    if cid:
                        surviving_call_ids.add(cid)

        result_call_ids: set[str] = set()
        for m in messages:
            if m.role == "tool":
                cid = m.tool_call_id or ""
                if cid:
                    result_call_ids.add(cid)

        # Drop orphaned results.
        orphaned = result_call_ids - surviving_call_ids
        if orphaned:
            messages = [
                m for m in messages
                if not (m.role == "tool" and (m.tool_call_id or "") in orphaned)
            ]
            if not self.quiet_mode:
                logger.info(
                    "ContextCompressor.sanitize.dropped_orphan_results count=%d",
                    len(orphaned),
                )

        # Add stub results for orphaned calls.
        missing = surviving_call_ids - result_call_ids
        if missing:
            patched: list[Message] = []
            for m in messages:
                patched.append(m)
                if m.role == "assistant":
                    for tc in m.tool_calls or ():
                        cid = getattr(tc, "id", "") or ""
                        if cid in missing:
                            patched.append(Message(
                                role="tool",
                                content=(
                                    "[Result from earlier conversation — "
                                    "see context summary above]"
                                ),
                                tool_call_id=cid,
                            ))
            messages = patched
            if not self.quiet_mode:
                logger.info(
                    "ContextCompressor.sanitize.added_stub_results count=%d",
                    len(missing),
                )

        return messages


__all__ = [
    "ContextCompressor",
    "SUMMARY_PREFIX",
    "estimate_messages_tokens_rough",
]
